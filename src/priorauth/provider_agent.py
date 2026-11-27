"""Provider agent: prepares and submits prior-authorization requests (LangGraph).

    requirements ─► patient_context ─► gather_evidence ─► draft_justification ─► submit ─► END
         │ (no policy)                     (FHIR MCP tools,          (LLM, PHI-checked)    (A2A)
         └────────────► END                 only what criteria need)

The agent talks to two MCP servers (payer policy, provider FHIR) and to the payer
agent over A2A. Everything that leaves the provider boundary is PHI-checked first.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, TypedDict

import httpx
from a2a.client.client import ClientConfig
from a2a.client.client_factory import create_client
from a2a.helpers.proto_helpers import get_artifact_text, get_data_parts, new_data_message
from a2a.types import Role, SendMessageRequest, TaskState
from langgraph.graph import END, StateGraph
from mcp import Client as MCPClient

from priorauth.audit import AuditLog
from priorauth.llm import LLM, LLMError, TemplateLLM
from priorauth.models import Evidence, PARequest
from priorauth.phi import assert_no_phi, identifiers_of

JUSTIFICATION_SYSTEM = ("You draft prior-authorization justifications for a clinician to review. Use only the "
                        "de-identified evidence provided. Be factual and concise. Do not invent findings.")

EVIDENCE_TOOLS: dict[str, tuple[str, list[str]]] = {
    "diagnosis": ("find_conditions", ["code_prefixes"]),
    "observation": ("find_observations", ["loinc", "within_days"]),
    "medication_duration": ("find_medications", ["ingredient"]),
    "procedure_span": ("find_procedures", ["code", "within_days"]),
    "allergy": ("find_allergies", ["substance"]),
}

STATE_NAMES = {TaskState.TASK_STATE_COMPLETED: "completed", TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
               TaskState.TASK_STATE_REJECTED: "rejected", TaskState.TASK_STATE_FAILED: "failed"}


class PAState(TypedDict, total=False):
    patient_id: str
    service_code: str
    request_id: str
    requirements: dict | None
    context: dict
    evidence: list[dict]
    gaps: list[str]
    justification: str
    submitted: dict
    task_id: str
    context_id: str
    task_state: str
    determination: dict | None
    letter: str
    error: str | None
    trace: list[str]


MCPFactory = Callable[[], AbstractAsyncContextManager[MCPClient]]


class ProviderAgent:
    def __init__(self, fhir: MCPFactory, policy: MCPFactory, payer_url: str, *,
                 llm: LLM | None = None, httpx_client: httpx.AsyncClient | None = None,
                 audit: AuditLog | None = None, phi_denylist: Callable[[str], set[str]] | None = None):
        self.fhir, self.policy, self.payer_url = fhir, policy, payer_url
        self.llm = llm or TemplateLLM()
        self.http = httpx_client
        self.audit = audit or AuditLog()
        self.phi_denylist = phi_denylist or (lambda _pid: set())
        self.graph = self._build()

    # ----------------------------------------------------------------- nodes
    async def _requirements(self, s: PAState) -> PAState:
        async with self.policy() as c:
            r = await c.call_tool("get_requirements", {"service_code": s["service_code"]})
        if r.is_error:
            return {"requirements": None, "error": r.content[0].text, "trace": [*s["trace"], "requirements -> none"]}
        return {"requirements": r.structured_content, "trace": [*s["trace"], "requirements (policy MCP)"]}

    async def _patient_context(self, s: PAState) -> PAState:
        async with self.fhir() as c:
            r = await c.call_tool("patient_context", {"patient_id": s["patient_id"]})
        if r.is_error:
            return {"error": r.content[0].text, "trace": [*s["trace"], "patient_context -> error"]}
        return {"context": r.structured_content, "trace": [*s["trace"], "patient_context (FHIR MCP)"]}

    async def _gather(self, s: PAState) -> PAState:
        evidence, found_for = [], set()
        calls = 0
        async with self.fhir() as c:
            for need in s["requirements"]["evidence_needed"]:
                tool, keys = EVIDENCE_TOOLS[need["type"]]
                args = {"patient_id": s["patient_id"], "criterion_id": need["criterion_id"],
                        **{k: need["params"][k] for k in keys}}
                r = await c.call_tool(tool, args)
                calls += 1
                if r.is_error:
                    raise RuntimeError(f"FHIR tool {tool} failed: {r.content[0].text}")
                items = r.structured_content["items"]
                evidence += items
                if items:
                    found_for.add(need["criterion_id"])
        all_ids = {n["criterion_id"] for n in s["requirements"]["evidence_needed"]}
        bypass_ids = {n["criterion_id"] for n in s["requirements"]["evidence_needed"] if n.get("bypass")}
        gaps = sorted(all_ids - found_for - bypass_ids)
        self.audit.record("provider-agent", "evidence_gathered", s["request_id"], evidence, tool_calls=calls)
        return {"evidence": evidence, "gaps": gaps,
                "trace": [*s["trace"], f"gather_evidence ({calls} FHIR MCP calls, {len(evidence)} items, gaps={gaps})"]}

    async def _draft(self, s: PAState) -> PAState:
        payload = json.dumps({"kind": "justification", "service_title": s["requirements"]["title"],
                              "age": s["context"]["age"], "sex": s["context"]["sex"],
                              "evidence": s["evidence"], "gaps": s["gaps"]})
        assert_no_phi(payload, self.phi_denylist(s["patient_id"]), "LLM prompt")
        try:
            text = self.llm.complete(JUSTIFICATION_SYSTEM, payload)
        except LLMError:
            text = TemplateLLM().complete(JUSTIFICATION_SYSTEM, payload)
        assert_no_phi(text, self.phi_denylist(s["patient_id"]), "LLM output")
        return {"justification": text, "trace": [*s["trace"], f"draft_justification ({self.llm.name})"]}

    async def _submit(self, s: PAState) -> PAState:
        req = PARequest(request_id=s["request_id"], member_ref=s["context"]["member_ref"], age=s["context"]["age"],
                        sex=s["context"]["sex"], service_code=s["service_code"],
                        evidence=[Evidence(**e) for e in s["evidence"]], justification=s["justification"])
        body = req.model_dump(mode="json")
        assert_no_phi(body, self.phi_denylist(s["patient_id"]), "A2A request")
        self.audit.record("provider-agent", "submitted", s["request_id"], body)
        result = await self._send(new_data_message(body, role=Role.ROLE_USER))
        return {**result, "submitted": body, "trace": [*s["trace"], f"submit (A2A) -> {result['task_state']}"]}

    # -------------------------------------------------------------- helpers
    async def _send(self, message) -> dict:
        client = await create_client(self.payer_url,
                                     client_config=ClientConfig(streaming=False, httpx_client=self.http))
        task = None
        async for resp in client.send_message(SendMessageRequest(message=message)):
            if resp.HasField("task"):
                task = resp.task
        if task is None:
            raise RuntimeError("Payer agent returned no task")
        determination, letter = None, ""
        for art in task.artifacts:
            data = get_data_parts(art.parts)
            if data:
                determination = data[0]
            letter = get_artifact_text(art) or letter
        status_data = get_data_parts(task.status.message.parts) if task.status.HasField("message") else []
        if determination is None and status_data:
            determination = status_data[0]
        return {"task_id": task.id, "context_id": task.context_id,
                "task_state": STATE_NAMES.get(task.status.state, str(task.status.state)),
                "determination": determination, "letter": letter}

    async def follow_up(self, state: PAState, extra_evidence: list[Evidence]) -> PAState:
        """Answer an INPUT_REQUIRED task with additional evidence, in the same A2A task."""
        body = {"request_id": state["request_id"], "evidence": [e.model_dump(mode="json") for e in extra_evidence]}
        assert_no_phi(body, self.phi_denylist(state["patient_id"]), "A2A follow-up")
        msg = new_data_message(body, role=Role.ROLE_USER, task_id=state["task_id"], context_id=state["context_id"])
        self.audit.record("provider-agent", "follow_up", state["request_id"], body)
        result = await self._send(msg)
        return {**state, **result, "trace": [*state["trace"], f"follow_up (A2A) -> {result['task_state']}"]}

    def _build(self):
        g = StateGraph(PAState)
        g.add_node("requirements", self._requirements)
        g.add_node("patient_context", self._patient_context)
        g.add_node("gather_evidence", self._gather)
        g.add_node("draft_justification", self._draft)
        g.add_node("submit", self._submit)
        g.set_entry_point("requirements")
        g.add_conditional_edges("requirements", lambda s: "ok" if s.get("requirements") else "stop",
                                {"ok": "patient_context", "stop": END})
        g.add_conditional_edges("patient_context", lambda s: "stop" if s.get("error") else "ok",
                                {"ok": "gather_evidence", "stop": END})
        g.add_edge("gather_evidence", "draft_justification")
        g.add_edge("draft_justification", "submit")
        g.add_edge("submit", END)
        return g.compile()

    async def run(self, patient_id: str, service_code: str) -> PAState:
        init: PAState = {"patient_id": patient_id, "service_code": service_code,
                         "request_id": f"PA-{uuid.uuid4().hex[:10]}", "trace": [], "error": None}
        return await self.graph.ainvoke(init)


def in_memory_agent(*, llm: LLM | None = None, audit: AuditLog | None = None,
                    payer_service: Any = None) -> tuple[ProviderAgent, Any]:
    """Wire provider agent, both MCP servers and the payer A2A app in one process (tests, demo, evals)."""
    from priorauth.mcp_fhir import FhirStore
    from priorauth.mcp_fhir import build_server as fhir_server
    from priorauth.mcp_policy import build_server as policy_server
    from priorauth.payer_agent import build_app

    store = FhirStore()
    fhir_srv, policy_srv = fhir_server(store), policy_server()
    app = build_app(payer_service, base_url="http://payer.local")
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://payer.local")
    agent = ProviderAgent(lambda: MCPClient(fhir_srv), lambda: MCPClient(policy_srv), "http://payer.local",
                          llm=llm, httpx_client=http, audit=audit,
                          phi_denylist=lambda pid: identifiers_of(store.patient(pid)))
    return agent, app
