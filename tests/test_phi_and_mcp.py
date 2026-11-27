import json

import pytest
from mcp import Client

from priorauth.mcp_fhir import FhirStore, build_server
from priorauth.mcp_policy import build_server as policy_server
from priorauth.phi import PHILeak, assert_no_phi, find_leaks, identifiers_of, pseudonym

STORE = FhirStore()


def test_pseudonym_is_stable_and_keyed(monkeypatch):
    a = pseudonym("mbr", "LHP-55001")
    assert a == pseudonym("mbr", "LHP-55001") and "55001" not in a
    monkeypatch.setenv("PA_PSEUDONYM_KEY", "another-key")
    assert pseudonym("mbr", "LHP-55001") != a


def test_identifiers_cover_all_direct_identifiers():
    ids = identifiers_of(STORE.patient("P001"))
    assert {"Arun Vel", "MRN-100001", "LHP-55001", "1968-04-12", "+91-90000-00001"} <= ids


def test_leak_detection_and_error_does_not_echo_value():
    deny = identifiers_of(STORE.patient("P001"))
    assert find_leaks({"note": "patient Arun Vel"}, deny)
    assert find_leaks("call +91-90000-12345", set())  # generic pattern, any patient
    with pytest.raises(PHILeak) as e:
        assert_no_phi({"mrn": "MRN-100001"}, deny, "test")
    assert "MRN-100001" not in str(e.value)


async def test_fhir_tools_return_no_direct_identifiers():
    deny = identifiers_of(STORE.patient("P009"))
    async with Client(build_server(STORE)) as c:
        outputs = [
            (await c.call_tool("patient_context", {"patient_id": "P009"})).structured_content,
            (await c.call_tool("find_conditions", {"patient_id": "P009", "criterion_id": "d", "code_prefixes": ["M05"]})).structured_content,
            (await c.call_tool("find_medications", {"patient_id": "P009", "criterion_id": "m", "ingredient": "methotrexate"})).structured_content,
            (await c.call_tool("find_observations", {"patient_id": "P009", "criterion_id": "t", "loinc": "71773-6", "within_days": 365})).structured_content,
        ]
    assert find_leaks(json.dumps(outputs), deny) == []
    assert outputs[0]["age"] == 60 and outputs[0]["member_ref"].startswith("mbr-")


async def test_fhir_tools_are_minimum_necessary():
    async with Client(build_server(STORE)) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        r = await c.call_tool("find_observations", {"patient_id": "P001", "criterion_id": "h", "loinc": "4548-4", "within_days": 10})
    assert "search" not in " ".join(tools) and "read_resource" not in tools  # no generic query surface
    assert r.structured_content["items"] == []  # HbA1c is 30 days old, outside the requested window


async def test_unknown_patient_is_a_clean_tool_error():
    async with Client(build_server(STORE)) as c:
        r = await c.call_tool("patient_context", {"patient_id": "P999"})
    assert r.is_error and "Unknown patient" in r.content[0].text


async def test_policy_server_marks_bypass_criteria():
    async with Client(policy_server()) as c:
        req = (await c.call_tool("get_requirements", {"service_code": "MRI-LSPINE"})).structured_content
        bad = await c.call_tool("get_requirements", {"service_code": "NOPE"})
    bypass = [n for n in req["evidence_needed"] if n["bypass"]]
    assert {n["criterion_id"] for n in bypass} == {"red_flag"}
    assert bad.is_error and "No prior-authorization policy" in bad.content[0].text
