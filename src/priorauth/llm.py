"""LLM providers. The LLM only drafts text: the clinical justification and the determination letter.

``TemplateLLM`` is the default, so the whole system runs offline with no API key.
Every prompt passes a PHI check in the calling agent before it reaches a provider.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Protocol

import httpx


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


class LLMError(RuntimeError):
    pass


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("PA_LLM_MODEL", "claude-sonnet-5-5")
        self.key = os.environ["ANTHROPIC_API_KEY"]

    def complete(self, system: str, user: str) -> str:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
                       headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                       json={"model": self.model, "max_tokens": 800, "temperature": 0, "system": system,
                             "messages": [{"role": "user", "content": user}]})
        if r.status_code != 200:
            raise LLMError(f"Anthropic API {r.status_code}: {r.text[:200]}")
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()


class GeminiLLM:
    name = "gemini"

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("PA_LLM_MODEL", "gemini-2.5-flash")
        self.key = os.environ["GEMINI_API_KEY"]

    def complete(self, system: str, user: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        r = httpx.post(url, timeout=60, headers={"x-goog-api-key": self.key},
                       json={"systemInstruction": {"parts": [{"text": system}]},
                             "contents": [{"role": "user", "parts": [{"text": user}]}],
                             "generationConfig": {"temperature": 0}})
        if r.status_code != 200:
            raise LLMError(f"Gemini API {r.status_code}: {r.text[:200]}")
        return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()


class TemplateLLM:
    """Deterministic, offline drafting. Produces plain, factual text from the structured payload."""

    name = "template"

    def complete(self, system: str, user: str) -> str:
        data = json.loads(user)
        if data.get("kind") == "justification":
            lines = [f"Request for {data['service_title']} for a {data['age']}-year-old {data['sex']} member."]
            for e in data["evidence"]:
                val = f" = {e['value']:g}{e.get('unit') or ''}" if e.get("value") is not None else ""
                when = f", {e['days_ago']} days ago" if e.get("days_ago") is not None else ""
                dur = f", {e['duration_days']} days of therapy" if e.get("duration_days") else ""
                lines.append(f"- {e['criterion_id']}: {e['display'] or e['code']}{val}{when}{dur}.")
            if data["gaps"]:
                lines.append("No supporting record was found for: " + ", ".join(data["gaps"]) + ".")
            return "\n".join(lines)
        if data.get("kind") == "letter":
            head = {"approved": "Your request has been approved.",
                    "pended_more_information": "We need more information before we can decide.",
                    "routed_to_clinical_review": "Your request has been sent to a clinician for review. "
                                                 "No decision to deny has been made.",
                    "not_covered_by_policy": "This service does not require or is not handled by prior authorization."}
            return " ".join([head[data["outcome"]], *data["reasons"]])
        raise LLMError("TemplateLLM received an unknown payload kind")


class ScriptedLLM:
    """Test double that records every prompt it receives."""

    name = "scripted"

    def __init__(self, handler: Callable[[str, str], str] | None = None):
        self.handler = handler or (lambda s, u: TemplateLLM().complete(s, u))
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.handler(system, user)


def from_env() -> LLM:
    provider = os.getenv("PA_LLM_PROVIDER", "template")
    return {"template": TemplateLLM, "anthropic": AnthropicLLM, "gemini": GeminiLLM}[provider]()
