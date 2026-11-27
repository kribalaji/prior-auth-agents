import json

import httpx
import pytest
from a2a.helpers.proto_helpers import new_data_message
from a2a.types import Role

from conftest import scenarios
from priorauth.audit import AuditLog
from priorauth.llm import ScriptedLLM
from priorauth.mcp_fhir import FhirStore
from priorauth.models import Evidence
from priorauth.phi import find_leaks, identifiers_of
from priorauth.provider_agent import in_memory_agent

STORE = FhirStore()


@pytest.mark.parametrize("case", scenarios(), ids=lambda c: f"{c['patient']}-{c['service']}")
async def test_scenario_outcomes(case):
    agent, _ = in_memory_agent()
    s = await agent.run(case["patient"], case["service"])
    assert s["determination"]["outcome"] == case["outcome"], case["why"]
    expected_state = "input_required" if case["outcome"] == "pended_more_information" else "completed"
    assert s["task_state"] == expected_state


async def test_no_phi_reaches_llm_or_payer():
    llm, sent = ScriptedLLM(), []
    agent, _ = in_memory_agent(llm=llm)

    async def capture(request: httpx.Request):
        if request.method == "POST":
            sent.append(request.content.decode())

    agent.http.event_hooks["request"].append(capture)
    for case in scenarios():
        await agent.run(case["patient"], case["service"])
    everything = [u for _, u in llm.calls] + sent
    assert len(llm.calls) == 12 and len(sent) == 12
    for case in scenarios():
        deny = identifiers_of(STORE.patient(case["patient"]))
        assert find_leaks(everything, deny) == [], case["patient"]


async def test_pended_task_resumes_with_follow_up_evidence():
    agent, _ = in_memory_agent()
    s = await agent.run("P010", "BIOLOGIC-RA")
    assert s["task_state"] == "input_required"
    igra = Evidence(criterion_id="tb_screen", resource_type="Observation", resource_ref="Observation/lab-new",
                    code="71773-6", display="IGRA", value=0, days_ago=3)
    s2 = await agent.follow_up(s, [igra])
    assert s2["task_id"] == s["task_id"]
    assert s2["task_state"] == "completed" and s2["determination"]["outcome"] == "approved"


async def test_clinical_review_queue_and_human_decision():
    audit = AuditLog()
    agent, app = in_memory_agent(audit=audit)
    await agent.run("P003", "GLP1-T2D")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://payer.local") as c:
        queue = (await c.get("/review-queue")).json()
        assert len(queue) == 1 and queue[0]["decision"] is None
        ticket = queue[0]["ticket"]
        assert (await c.post(f"/review-queue/{ticket}/decision", json={"decision": "deny"})).status_code == 422
        r = await c.post(f"/review-queue/{ticket}/decision", json={"decision": "approve", "reviewer": "dr-meena"})
        assert r.json()["decision"] == "approve"
    payer_events = app.state.service.audit.events
    assert any(e["actor"] == "clinician:dr-meena" for e in payer_events)
    assert any(e["action"] == "submitted" for e in audit.events)


async def test_payer_rejects_direct_identifiers_and_bad_payloads():
    agent, app = in_memory_agent()
    bad = {"request_id": "x", "member_ref": "MRN-100001", "age": 50, "sex": "male",
           "service_code": "GLP1-T2D", "evidence": [], "justification": ""}
    r = await agent._send(new_data_message(bad, role=Role.ROLE_USER))
    assert r["task_state"] == "rejected" and "direct identifiers" in json.dumps(r["determination"])
    r = await agent._send(new_data_message({"hello": "world"}, role=Role.ROLE_USER))
    assert r["task_state"] == "rejected"


async def test_unknown_service_stops_before_touching_patient_data():
    agent, _ = in_memory_agent()
    s = await agent.run("P001", "COSMETIC-XYZ")
    assert s["requirements"] is None and "No prior-authorization policy" in s["error"]
    assert "context" not in s and "evidence" not in s


async def test_agent_card_advertises_a2a_1_0_jsonrpc():
    _, app = in_memory_agent()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://payer.local") as c:
        card = (await c.get("/.well-known/agent-card.json")).json()
    iface = card["supportedInterfaces"][0]
    assert iface["protocolBinding"] == "JSONRPC" and iface["protocolVersion"] == "1.0"
    assert card["skills"][0]["id"] == "prior-authorization"


async def test_leaking_llm_output_is_blocked_before_submission():
    from priorauth.phi import PHILeak

    sent = []
    leaky = ScriptedLLM(lambda s, u: "Justification for Arun Vel (MRN-100001): meets criteria.")
    agent, _ = in_memory_agent(llm=leaky)
    agent.http.event_hooks["request"].append(lambda r: sent.append(r) if r.method == "POST" else None)
    with pytest.raises(PHILeak):
        await agent.run("P001", "GLP1-T2D")
    assert sent == []  # nothing reached the payer
