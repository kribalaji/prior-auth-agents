"""Deterministic policy evaluation.

The rules engine is the only component that decides whether a criterion is met.
LLMs never decide coverage. An unmet criterion never produces an automatic denial:
it routes the case to a human clinical reviewer.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from priorauth.models import CriterionResult, CriterionStatus, Determination, Evidence, Outcome

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
       "<": lambda a, b: a < b, "==": lambda a, b: a == b}


class PolicyBook:
    def __init__(self, spec: dict):
        self.spec = spec
        self.by_id = {p["id"]: p for p in spec["policies"]}

    @classmethod
    def load(cls, path: str | Path) -> PolicyBook:
        return cls(yaml.safe_load(Path(path).read_text()))

    @property
    def as_of(self) -> str:
        return self.spec["as_of"]

    def get(self, service_code: str) -> dict | None:
        return self.by_id.get(service_code)


def leaf_criteria(criterion: dict) -> Iterable[dict]:
    """Flatten any_of trees into the leaf criteria a provider must gather evidence for."""
    if criterion["type"] == "any_of":
        for c in criterion["of"]:
            yield from leaf_criteria(c)
    else:
        yield criterion


def _evaluate(c: dict, ev: list[Evidence]) -> tuple[CriterionStatus, str, list[str]]:
    t = c["type"]
    if t == "any_of":
        results = [_evaluate(sub, ev) for sub in c["of"]]
        met = [r for r in results if r[0] is CriterionStatus.MET]
        if met:
            return CriterionStatus.MET, met[0][1], met[0][2]
        if any(r[0] is CriterionStatus.MISSING for r in results):
            return CriterionStatus.MISSING, "; ".join(r[1] for r in results), []
        return CriterionStatus.NOT_MET, "; ".join(r[1] for r in results), []

    if t == "diagnosis":
        hits = [e for e in ev if e.resource_type == "Condition"
                and any(e.code.startswith(p) for p in c["code_prefixes"])]
        if hits:
            return CriterionStatus.MET, f"diagnosis {hits[0].code} documented", [h.resource_ref for h in hits]
        return CriterionStatus.NOT_MET, f"no diagnosis matching {c['code_prefixes']}", []

    if t == "observation":
        obs = [e for e in ev if e.resource_type == "Observation" and e.code == c["loinc"]
               and e.days_ago is not None and e.days_ago <= c["within_days"] and e.value is not None]
        if not obs:
            return CriterionStatus.MISSING, f"no LOINC {c['loinc']} result in last {c['within_days']} days", []
        latest = min(obs, key=lambda e: e.days_ago)
        ok = OPS[c["op"]](latest.value, c["value"])
        status = CriterionStatus.MET if ok else CriterionStatus.NOT_MET
        return status, f"latest {latest.display or c['loinc']} = {latest.value:g} ({latest.days_ago} days ago); " \
                       f"requires {c['op']} {c['value']}", [latest.resource_ref]

    if t == "medication_duration":
        meds = [e for e in ev if e.resource_type == "MedicationStatement"
                and c["ingredient"] in e.display.lower() and e.duration_days is not None]
        if not meds:
            return CriterionStatus.NOT_MET, f"no {c['ingredient']} history", []
        best = max(meds, key=lambda e: e.duration_days)
        ok = best.duration_days >= c["min_days"]
        return (CriterionStatus.MET if ok else CriterionStatus.NOT_MET,
                f"{c['ingredient']} for {best.duration_days} days; requires {c['min_days']}", [best.resource_ref])

    if t == "procedure_span":
        procs = [e for e in ev if e.resource_type == "Procedure" and e.code == c["code"]
                 and e.days_ago is not None and e.days_ago <= c["within_days"]]
        if not procs:
            return CriterionStatus.NOT_MET, f"no procedure {c['code']} in last {c['within_days']} days", []
        span = max(e.days_ago for e in procs) - min(e.days_ago for e in procs)
        ok = span >= c["min_days"]
        return (CriterionStatus.MET if ok else CriterionStatus.NOT_MET,
                f"{len(procs)} sessions of {c['code']} over {span} days; requires {c['min_days']}",
                [e.resource_ref for e in procs])

    if t == "allergy":
        hits = [e for e in ev if e.resource_type == "AllergyIntolerance" and c["substance"] in e.display.lower()]
        if hits:
            return CriterionStatus.MET, f"documented {c['substance']} intolerance", [hits[0].resource_ref]
        return CriterionStatus.NOT_MET, f"no documented {c['substance']} intolerance", []

    raise ValueError(f"Unknown criterion type: {t}")


def evaluate(policies: PolicyBook, request_id: str, service_code: str, evidence: list[Evidence]) -> Determination:
    policy = policies.get(service_code)
    if policy is None:
        return Determination(request_id=request_id, service_code=service_code, policy_id=None,
                             outcome=Outcome.NOT_COVERED, criteria=[],
                             reasons=[f"No prior-authorization policy for service {service_code}"])

    for b in policy.get("bypass", []):
        status, detail, refs = _evaluate(b, evidence)
        if status is CriterionStatus.MET:
            r = CriterionResult(criterion_id=b["id"], text=b["text"], status=status, detail=detail, evidence_refs=refs)
            return Determination(request_id=request_id, service_code=service_code, policy_id=policy["id"],
                                 outcome=Outcome.APPROVED, criteria=[r],
                                 reasons=[f"Approved on bypass criterion '{b['id']}': {detail}"])

    results = []
    for c in policy["criteria"]:
        status, detail, refs = _evaluate(c, evidence)
        results.append(CriterionResult(criterion_id=c["id"], text=c["text"], status=status,
                                       detail=detail, evidence_refs=refs))

    missing = [r.text for r in results if r.status is CriterionStatus.MISSING]
    unmet = [r for r in results if r.status is CriterionStatus.NOT_MET]
    if unmet:
        outcome = Outcome.CLINICAL_REVIEW
        reasons = [f"Criterion '{r.criterion_id}' not met: {r.detail}" for r in unmet]
    elif missing:
        outcome = Outcome.PENDED_INFO
        reasons = [f"Additional information needed: {m}" for m in missing]
    else:
        outcome = Outcome.APPROVED
        reasons = ["All policy criteria met"]
    return Determination(request_id=request_id, service_code=service_code, policy_id=policy["id"],
                         outcome=outcome, criteria=results, reasons=reasons, missing=missing)
