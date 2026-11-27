"""Write fictional FHIR R4 patient bundles to data/fhir/.

Every patient, name and identifier here is invented. Each patient is a test scenario
whose expected prior-authorization outcome is listed in evals/scenarios.yaml.
Dates are computed relative to the policy as-of date so the dataset never goes stale.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 11, 1)
OUT = Path(__file__).resolve().parents[1] / "data" / "fhir"


def d(days_ago: int) -> str:
    return (AS_OF - timedelta(days=days_ago)).isoformat()


class Builder:
    def __init__(self, pid: str, name: str, birth: str, sex: str, mrn: str, member_id: str, phone: str):
        self.pid, self.n = pid, 0
        self.entries = [{"resource": {
            "resourceType": "Patient", "id": pid,
            "identifier": [{"system": "urn:larkspur:mrn", "value": mrn},
                           {"system": "urn:larkspur:member", "value": member_id}],
            "name": [{"family": name.split()[-1], "given": name.split()[:-1]}],
            "gender": sex, "birthDate": birth,
            "telecom": [{"system": "phone", "value": phone}],
            "address": [{"city": "Chennai", "country": "IN"}],
        }}]

    def _id(self, kind: str) -> str:
        self.n += 1
        return f"{self.pid}-{kind}-{self.n}"

    def _add(self, res: dict) -> Builder:
        res["subject"] = {"reference": f"Patient/{self.pid}"}
        self.entries.append({"resource": res})
        return self

    def condition(self, code: str, display: str, onset_days_ago: int = 400) -> Builder:
        return self._add({"resourceType": "Condition", "id": self._id("cond"),
                          "clinicalStatus": {"coding": [{"code": "active"}]},
                          "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": code,
                                               "display": display}]},
                          "onsetDateTime": d(onset_days_ago)})

    def observation(self, loinc: str, display: str, value: float, unit: str, days_ago: int) -> Builder:
        return self._add({"resourceType": "Observation", "id": self._id("obs"), "status": "final",
                          "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": display}]},
                          "valueQuantity": {"value": value, "unit": unit}, "effectiveDateTime": d(days_ago)})

    def medication(self, name: str, start_days_ago: int, end_days_ago: int = 0) -> Builder:
        return self._add({"resourceType": "MedicationStatement", "id": self._id("med"), "status": "active",
                          "medicationCodeableConcept": {"text": name},
                          "effectivePeriod": {"start": d(start_days_ago), "end": d(end_days_ago)}})

    def procedure(self, cpt: str, display: str, days_ago: list[int]) -> Builder:
        for x in days_ago:
            self._add({"resourceType": "Procedure", "id": self._id("proc"), "status": "completed",
                       "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": cpt,
                                            "display": display}]},
                       "performedDateTime": d(x)})
        return self

    def allergy(self, substance: str, reaction: str) -> Builder:
        return self._add({"resourceType": "AllergyIntolerance", "id": self._id("alg"),
                          "code": {"text": substance}, "reaction": [{"description": reaction}],
                          "patient": {"reference": f"Patient/{self.pid}"}})

    def bundle(self) -> dict:
        return {"resourceType": "Bundle", "type": "collection", "entry": self.entries}


def weekly(start: int, weeks: int) -> list[int]:
    return [start - 7 * i for i in range(weeks)]


def scenarios() -> list[Builder]:
    t2d = ("E11.9", "Type 2 diabetes mellitus without complications")
    lbp = ("M54.50", "Low back pain, unspecified")
    ra = ("M05.79", "Rheumatoid arthritis with rheumatoid factor, multiple sites")
    a1c = ("4548-4", "Hemoglobin A1c")
    igra = ("71773-6", "Mycobacterium tuberculosis IGRA")
    pt = ("97110", "Therapeutic exercise (physical therapy)")
    return [
        Builder("P001", "Arun Vel", "1968-04-12", "male", "MRN-100001", "LHP-55001", "+91-90000-00001")
        .condition(*t2d).observation(*a1c, 8.1, "%", 30).medication("Metformin 1000 mg", 200),
        Builder("P002", "Meena Raj", "1975-09-03", "female", "MRN-100002", "LHP-55002", "+91-90000-00002")
        .condition(*t2d).observation(*a1c, 8.4, "%", 400).medication("Metformin 500 mg", 220),
        Builder("P003", "Suresh Iyer", "1960-01-22", "male", "MRN-100003", "LHP-55003", "+91-90000-00003")
        .condition(*t2d).observation(*a1c, 6.4, "%", 45).medication("Metformin 1000 mg", 300),
        Builder("P004", "Lakshmi Nair", "1971-07-30", "female", "MRN-100004", "LHP-55004", "+91-90000-00004")
        .condition(*t2d).observation(*a1c, 7.6, "%", 20).allergy("Metformin", "Severe gastrointestinal intolerance"),
        Builder("P005", "Karthik Rao", "1985-11-11", "male", "MRN-100005", "LHP-55005", "+91-90000-00005")
        .condition(*lbp, onset_days_ago=120).procedure(*pt, weekly(100, 9)),
        Builder("P006", "Divya Menon", "1990-02-14", "female", "MRN-100006", "LHP-55006", "+91-90000-00006")
        .condition(*lbp, onset_days_ago=40).procedure(*pt, weekly(20, 3)),
        Builder("P007", "Ravi Shankar", "1958-06-05", "male", "MRN-100007", "LHP-55007", "+91-90000-00007")
        .condition(*lbp, onset_days_ago=10).condition("G83.4", "Cauda equina syndrome", onset_days_ago=3),
        Builder("P008", "Priya Das", "1982-12-01", "female", "MRN-100008", "LHP-55008", "+91-90000-00008")
        .condition("M25.561", "Pain in right knee").procedure(*pt, weekly(90, 8)),
        Builder("P009", "Anand Kumar", "1966-03-19", "male", "MRN-100009", "LHP-55009", "+91-90000-00009")
        .condition(*ra).medication("Methotrexate 15 mg weekly", 120).observation(*igra, 0, "{result}", 60),
        Builder("P010", "Fathima Begum", "1979-08-25", "female", "MRN-100010", "LHP-55010", "+91-90000-00010")
        .condition(*ra).medication("Methotrexate 20 mg weekly", 150),
        Builder("P011", "Vijay Pillai", "1973-05-08", "male", "MRN-100011", "LHP-55011", "+91-90000-00011")
        .condition(*ra).medication("Methotrexate 10 mg weekly", 28).observation(*igra, 0, "{result}", 30),
        Builder("P012", "Sneha Joshi", "1995-10-17", "female", "MRN-100012", "LHP-55012", "+91-90000-00012")
        .condition(*lbp, onset_days_ago=200).medication("Naproxen 500 mg", 150, 60),
    ]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for b in scenarios():
        (OUT / f"{b.pid}.json").write_text(json.dumps(b.bundle(), indent=2))
    print(f"wrote {len(scenarios())} bundles to {OUT}")


if __name__ == "__main__":
    main()
