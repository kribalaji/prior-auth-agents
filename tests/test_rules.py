from priorauth.models import CriterionStatus, Evidence, Outcome
from priorauth.rules import evaluate


def ev(**kw) -> Evidence:
    base = {"criterion_id": "x", "resource_ref": "r/1", "display": ""}
    return Evidence(**{**base, **kw})


T2D = ev(resource_type="Condition", code="E11.9")
A1C_HIGH = ev(resource_type="Observation", code="4548-4", value=8.0, days_ago=10)
METFORMIN_120 = ev(resource_type="MedicationStatement", code="metformin", display="Metformin 500 mg", duration_days=120)


def test_all_met_is_approved(book):
    d = evaluate(book, "r", "GLP1-T2D", [T2D, A1C_HIGH, METFORMIN_120])
    assert d.outcome is Outcome.APPROVED
    assert all(c.status is CriterionStatus.MET for c in d.criteria)


def test_missing_lab_pends_instead_of_denying(book):
    d = evaluate(book, "r", "GLP1-T2D", [T2D, METFORMIN_120])
    assert d.outcome is Outcome.PENDED_INFO
    assert any("HbA1c" in m for m in d.missing)


def test_stale_lab_counts_as_missing(book):
    stale = ev(resource_type="Observation", code="4548-4", value=9.0, days_ago=181)
    assert evaluate(book, "r", "GLP1-T2D", [T2D, stale, METFORMIN_120]).outcome is Outcome.PENDED_INFO


def test_latest_lab_wins(book):
    old_high = ev(resource_type="Observation", code="4548-4", value=9.0, days_ago=150, resource_ref="o/1")
    new_low = ev(resource_type="Observation", code="4548-4", value=6.5, days_ago=5, resource_ref="o/2")
    d = evaluate(book, "r", "GLP1-T2D", [T2D, old_high, new_low, METFORMIN_120])
    assert d.outcome is Outcome.CLINICAL_REVIEW


def test_unmet_criterion_routes_to_human_never_denies(book):
    short = ev(resource_type="MedicationStatement", code="metformin", display="Metformin", duration_days=30)
    d = evaluate(book, "r", "GLP1-T2D", [T2D, A1C_HIGH, short])
    assert d.outcome is Outcome.CLINICAL_REVIEW
    assert "deny" not in d.outcome.value


def test_any_of_accepts_intolerance(book):
    allergy = ev(resource_type="AllergyIntolerance", code="metformin", display="Metformin intolerance")
    assert evaluate(book, "r", "GLP1-T2D", [T2D, A1C_HIGH, allergy]).outcome is Outcome.APPROVED


def test_red_flag_bypass(book):
    cauda = ev(resource_type="Condition", code="G83.4")
    d = evaluate(book, "r", "MRI-LSPINE", [cauda])
    assert d.outcome is Outcome.APPROVED and d.criteria[0].criterion_id == "red_flag"


def test_procedure_span_threshold(book):
    lbp = ev(resource_type="Condition", code="M54.50")
    sessions = [ev(resource_type="Procedure", code="97110", days_ago=x, resource_ref=f"p/{x}") for x in (50, 30, 8)]
    assert evaluate(book, "r", "MRI-LSPINE", [lbp, *sessions]).outcome is Outcome.APPROVED  # span 42
    assert evaluate(book, "r", "MRI-LSPINE", [lbp, *sessions[1:]]).outcome is Outcome.CLINICAL_REVIEW  # span 22


def test_unknown_service_not_covered(book):
    d = evaluate(book, "r", "COSMETIC-XYZ", [])
    assert d.outcome is Outcome.NOT_COVERED and d.policy_id is None
