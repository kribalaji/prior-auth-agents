"""Typed messages exchanged between the provider agent, the payer agent and the rules engine."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """One de-identified clinical fact, traceable to a FHIR resource on the provider side."""

    criterion_id: str
    resource_type: str
    resource_ref: str  # pseudonymous reference, e.g. "Observation/3f9a1c2b"
    code: str
    display: str = ""
    value: float | None = None
    unit: str | None = None
    days_ago: int | None = None  # relative dates avoid sending exact service dates
    duration_days: int | None = None


class PARequest(BaseModel):
    """What the provider agent sends to the payer over A2A (minimum necessary)."""

    request_id: str
    member_ref: str = Field(description="Pseudonymous member reference, never name or MRN")
    age: int
    sex: str
    service_code: str
    evidence: list[Evidence]
    justification: str


class CriterionStatus(StrEnum):
    MET = "met"
    NOT_MET = "not_met"
    MISSING = "missing_evidence"


class CriterionResult(BaseModel):
    criterion_id: str
    text: str
    status: CriterionStatus
    detail: str
    evidence_refs: list[str] = Field(default_factory=list)


class Outcome(StrEnum):
    APPROVED = "approved"
    PENDED_INFO = "pended_more_information"
    CLINICAL_REVIEW = "routed_to_clinical_review"
    NOT_COVERED = "not_covered_by_policy"


class Determination(BaseModel):
    request_id: str
    service_code: str
    policy_id: str | None
    outcome: Outcome
    criteria: list[CriterionResult]
    reasons: list[str]
    missing: list[str] = Field(default_factory=list)
    letter: str = ""
