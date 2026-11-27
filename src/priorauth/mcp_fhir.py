"""FHIR MCP server (provider side).

Exposes narrow, purpose-built tools instead of a generic FHIR query endpoint, so an
agent can only fetch the facts a policy criterion needs (minimum necessary). Every
response is already de-identified: pseudonymous references, relative dates, no names.

Run standalone:  python -m priorauth.mcp_fhir   (stdio transport)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from priorauth.models import Evidence
from priorauth.phi import pseudonym

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "fhir"


class PatientContext(BaseModel):
    member_ref: str
    age: int
    sex: str


class EvidenceList(BaseModel):
    items: list[Evidence]


class FhirStore:
    def __init__(self, data_dir: Path = DEFAULT_DATA, as_of: date = date(2026, 11, 1)):
        self.as_of = as_of
        self.bundles = {p.stem: json.loads(p.read_text()) for p in sorted(data_dir.glob("*.json"))}

    def resources(self, patient_id: str, rtype: str) -> list[dict]:
        if patient_id not in self.bundles:
            raise ToolError(f"Unknown patient {patient_id}")
        return [e["resource"] for e in self.bundles[patient_id]["entry"] if e["resource"]["resourceType"] == rtype]

    def patient(self, patient_id: str) -> dict:
        return self.resources(patient_id, "Patient")[0]

    def days_ago(self, iso: str) -> int:
        return (self.as_of - date.fromisoformat(iso[:10])).days

    @staticmethod
    def ref(res: dict) -> str:
        return f"{res['resourceType']}/{pseudonym('res', res['id'], 10)}"


def build_server(store: FhirStore | None = None) -> MCPServer:
    store = store or FhirStore()
    mcp = MCPServer("fhir-provider")

    @mcp.tool()
    def patient_context(patient_id: str) -> PatientContext:
        """Pseudonymous member reference, age and sex. Never returns name, MRN, birth date or contact details."""
        p = store.patient(patient_id)
        birth = date.fromisoformat(p["birthDate"])
        age = store.as_of.year - birth.year - ((store.as_of.month, store.as_of.day) < (birth.month, birth.day))
        member = next(i["value"] for i in p["identifier"] if i["system"].endswith("member"))
        return PatientContext(member_ref=pseudonym("mbr", member), age=age, sex=p["gender"])

    @mcp.tool()
    def find_conditions(patient_id: str, criterion_id: str, code_prefixes: list[str]) -> EvidenceList:
        """Active conditions whose ICD-10 code starts with one of the given prefixes."""
        out = []
        for c in store.resources(patient_id, "Condition"):
            coding = c["code"]["coding"][0]
            if any(coding["code"].startswith(p) for p in code_prefixes):
                out.append(Evidence(criterion_id=criterion_id, resource_type="Condition", resource_ref=store.ref(c),
                                    code=coding["code"], display=coding.get("display", ""),
                                    days_ago=store.days_ago(c["onsetDateTime"])))
        return EvidenceList(items=out)

    @mcp.tool()
    def find_observations(patient_id: str, criterion_id: str, loinc: str, within_days: int) -> EvidenceList:
        """Observations with the given LOINC code from the last `within_days` days."""
        out = []
        for o in store.resources(patient_id, "Observation"):
            coding = o["code"]["coding"][0]
            age = store.days_ago(o["effectiveDateTime"])
            if coding["code"] == loinc and age <= within_days:
                q = o["valueQuantity"]
                out.append(Evidence(criterion_id=criterion_id, resource_type="Observation", resource_ref=store.ref(o),
                                    code=loinc, display=coding.get("display", ""), value=q["value"],
                                    unit=q.get("unit"), days_ago=age))
        return EvidenceList(items=out)

    @mcp.tool()
    def find_medications(patient_id: str, criterion_id: str, ingredient: str) -> EvidenceList:
        """Medication statements containing the ingredient, with treatment duration in days."""
        out = []
        for m in store.resources(patient_id, "MedicationStatement"):
            text = m["medicationCodeableConcept"]["text"]
            if ingredient.lower() in text.lower():
                start, end = m["effectivePeriod"]["start"], m["effectivePeriod"]["end"]
                out.append(Evidence(criterion_id=criterion_id, resource_type="MedicationStatement",
                                    resource_ref=store.ref(m), code=ingredient.lower(), display=text,
                                    days_ago=store.days_ago(end),
                                    duration_days=store.days_ago(start) - store.days_ago(end)))
        return EvidenceList(items=out)

    @mcp.tool()
    def find_procedures(patient_id: str, criterion_id: str, code: str, within_days: int) -> EvidenceList:
        """Completed procedures with the CPT code from the last `within_days` days."""
        out = []
        for p in store.resources(patient_id, "Procedure"):
            coding = p["code"]["coding"][0]
            age = store.days_ago(p["performedDateTime"])
            if coding["code"] == code and age <= within_days:
                out.append(Evidence(criterion_id=criterion_id, resource_type="Procedure", resource_ref=store.ref(p),
                                    code=code, display=coding.get("display", ""), days_ago=age))
        return EvidenceList(items=out)

    @mcp.tool()
    def find_allergies(patient_id: str, criterion_id: str, substance: str) -> EvidenceList:
        """Documented allergies or intolerances to the substance."""
        out = []
        for a in store.resources(patient_id, "AllergyIntolerance"):
            if substance.lower() in a["code"]["text"].lower():
                out.append(Evidence(criterion_id=criterion_id, resource_type="AllergyIntolerance",
                                    resource_ref=store.ref(a), code=substance.lower(),
                                    display=f"{a['code']['text']} intolerance"))
        return EvidenceList(items=out)

    return mcp


if __name__ == "__main__":
    build_server().run()
