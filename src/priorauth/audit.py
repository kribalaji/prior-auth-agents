"""Append-only audit trail. Records who did what to which pseudonymous request, plus a payload hash."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path


class AuditLog:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def record(self, actor: str, action: str, request_id: str, payload: object = None, **extra) -> dict:
        body = json.dumps(payload, sort_keys=True, default=str) if payload is not None else ""
        event = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor": actor,
            "action": action,
            "request_id": request_id,
            "payload_sha256": hashlib.sha256(body.encode()).hexdigest() if body else None,
            **extra,
        }
        with self._lock:
            self.events.append(event)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(json.dumps(event) + "\n")
        return event
