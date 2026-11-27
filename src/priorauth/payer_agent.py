"""Payer agent: an A2A 1.0 server that receives prior-authorization requests.

Task state mapping:
  approved                   -> COMPLETED      (determination artifact)
  pended_more_information    -> INPUT_REQUIRED (provider may answer in the same task)
  routed_to_clinical_review  -> COMPLETED      (queued for a human reviewer; never an automatic denial)
  not covered / invalid      -> REJECTED

Run:  uvicorn priorauth.payer_agent:app --port 9100
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from a2a.helpers.proto_helpers import get_data_parts, new_data_message, new_data_part, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill, TaskState
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from priorauth.audit import AuditLog
from priorauth.llm import LLM, LLMError, TemplateLLM
from priorauth.models import Determination, Evidence, Outcome, PARequest
from priorauth.phi import GENERIC_PATTERNS
from priorauth.rules import PolicyBook, evaluate

POLICIES = Path(__file__).resolve().parents[2] / "policies" / "policies.yaml"
LETTER_SYSTEM = ("Write a short, plain-language prior-authorization notice for a health plan member. "
                 "Use only the outcome and reasons given. Do not add clinical advice or new facts.")


class FollowUp(BaseModel):
    """Additional evidence sent by the provider in reply to an INPUT_REQUIRED task."""

    request_id: str
    evidence: list[Evidence]


@dataclass
class ReviewItem:
    ticket: str
    request: PARequest
    determination: Determination
    decision: str | None = None
    reviewer: str | None = None
    note: str | None = None


@dataclass
class PayerService:
    policies: PolicyBook
    llm: LLM = field(default_factory=TemplateLLM)
    audit: AuditLog = field(default_factory=AuditLog)
    open_requests: dict[str, PARequest] = field(default_factory=dict)  # task_id -> request awaiting input
    review_queue: dict[str, ReviewItem] = field(default_factory=dict)

    def letter(self, d: Determination) -> str:
        payload = json.dumps({"kind": "letter", "outcome": d.outcome.value, "reasons": d.reasons})
        try:
            return self.llm.complete(LETTER_SYSTEM, payload)
        except LLMError:
            return TemplateLLM().complete(LETTER_SYSTEM, payload)

    def decide(self, req: PARequest) -> Determination:
        d = evaluate(self.policies, req.request_id, req.service_code, req.evidence)
        d.letter = self.letter(d)
        if d.outcome is Outcome.CLINICAL_REVIEW:
            ticket = f"REV-{uuid.uuid4().hex[:8]}"
            self.review_queue[ticket] = ReviewItem(ticket, req, d)
            d.reasons.append(f"Clinical review ticket {ticket}")
        self.audit.record("payer-agent", f"determination:{d.outcome.value}", req.request_id, d.model_dump())
        return d


class PayerExecutor(AgentExecutor):
    def __init__(self, service: PayerService):
        self.svc = service

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED,
                                                     history=[context.message]))
        await updater.start_work()

        data = get_data_parts(context.message.parts)
        if not data:
            await updater.reject(self._msg(context, {"error": "Expected one JSON data part"}))
            return
        payload = _normalise(data[0])

        leaks = [m for p in GENERIC_PATTERNS for m in p.findall(json.dumps(payload))]
        if leaks:
            self.svc.audit.record("payer-agent", "rejected:direct_identifiers", payload.get("request_id", "?"))
            await updater.reject(self._msg(context, {"error": "Request contains direct identifiers; "
                                                              "send pseudonymous references only"}))
            return

        try:
            if task_id in self.svc.open_requests:  # resumed task: merge follow-up evidence
                follow = FollowUp.model_validate(payload)
                req = self.svc.open_requests.pop(task_id)
                req = req.model_copy(update={"evidence": [*req.evidence, *follow.evidence]})
            else:
                req = PARequest.model_validate(payload)
        except ValidationError as e:
            await updater.reject(self._msg(context, {"error": "Invalid request", "detail": e.errors()[:3]}))
            return

        self.svc.audit.record("payer-agent", "received", req.request_id, req.model_dump())
        d = self.svc.decide(req)
        if d.outcome is Outcome.NOT_COVERED:
            await updater.reject(self._msg(context, d.model_dump(mode="json")))
            return
        await updater.add_artifact([new_data_part(d.model_dump(mode="json")), new_text_part(d.letter)],
                                   name="determination")
        if d.outcome is Outcome.PENDED_INFO:
            self.svc.open_requests[task_id] = req
            await updater.requires_input(self._msg(context, {"missing": d.missing}))
        else:
            await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.svc.open_requests.pop(context.task_id, None)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    @staticmethod
    def _msg(context: RequestContext, data: dict):
        return new_data_message(data, task_id=context.task_id, context_id=context.context_id)


def _normalise(obj):
    """A2A data parts travel as protobuf Struct, where every number is a float. Restore integers."""
    if isinstance(obj, dict):
        return {k: _normalise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise(v) for v in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


def agent_card(base_url: str) -> AgentCard:
    return AgentCard(
        name="Larkspur Health Plan: Prior Authorization Agent",
        description="Receives prior-authorization requests, checks them against published coverage policy and "
                    "returns a determination. Unmet criteria are routed to a human clinical reviewer.",
        version="0.1.0",
        provider=AgentProvider(organization="Larkspur Health Plan (fictional)", url=base_url),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0",
                                             url=f"{base_url}/a2a")],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json", "text/plain"],
        skills=[AgentSkill(id="prior-authorization", name="Prior authorization determination",
                           description="Evaluate a de-identified evidence packet against coverage criteria.",
                           tags=["healthcare", "prior-authorization", "payer"],
                           input_modes=["application/json"], output_modes=["application/json"])],
    )


def build_app(service: PayerService | None = None, base_url: str = "http://localhost:9100") -> Starlette:
    service = service or PayerService(PolicyBook.load(POLICIES))
    card = agent_card(base_url)
    handler = DefaultRequestHandler(agent_executor=PayerExecutor(service), task_store=InMemoryTaskStore(),
                                    agent_card=card)

    async def list_queue(_: Request) -> JSONResponse:
        return JSONResponse([{"ticket": i.ticket, "service_code": i.request.service_code,
                              "member_ref": i.request.member_ref, "reasons": i.determination.reasons,
                              "decision": i.decision} for i in service.review_queue.values()])

    async def decide(request: Request) -> JSONResponse:
        item = service.review_queue.get(request.path_params["ticket"])
        if item is None:
            return JSONResponse({"error": "unknown ticket"}, status_code=404)
        body = await request.json()
        if body.get("decision") not in ("approve", "deny") or not body.get("reviewer"):
            return JSONResponse({"error": "decision must be approve|deny and reviewer is required"}, status_code=422)
        item.decision, item.reviewer, item.note = body["decision"], body["reviewer"], body.get("note")
        service.audit.record(f"clinician:{item.reviewer}", f"review:{item.decision}", item.request.request_id,
                             body, ticket=item.ticket)
        return JSONResponse({"ticket": item.ticket, "decision": item.decision})

    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/a2a"),
              Route("/review-queue", list_queue), Route("/review-queue/{ticket}/decision", decide, methods=["POST"])]
    app = Starlette(routes=routes)
    app.state.service = service
    return app


app = build_app()
