"""PHI controls: pseudonymisation, minimum-necessary projection and leak detection.

Direct identifiers (name, MRN, member ID, phone, birth date, address) stay inside the
provider's FHIR boundary. Outbound data carries keyed pseudonyms, age instead of birth
date, and relative day offsets instead of service dates.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterable

_DEV_KEY = "dev-only-pseudonym-key"  # override with PA_PSEUDONYM_KEY outside local development


def _key() -> bytes:
    return os.getenv("PA_PSEUDONYM_KEY", _DEV_KEY).encode()


def pseudonym(kind: str, value: str, length: int = 12) -> str:
    """Stable keyed pseudonym. Without the key, the original value cannot be recovered or guessed."""
    digest = hmac.new(_key(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
    return f"{kind}-{digest[:length]}"


def identifiers_of(patient: dict) -> set[str]:
    """Every direct identifier in a FHIR Patient resource, used as the leak-check denylist."""
    ids = {patient["id"], patient.get("birthDate", "")}
    for ident in patient.get("identifier", []):
        ids.add(ident["value"])
    for n in patient.get("name", []):
        ids.add(n.get("family", ""))
        ids.update(n.get("given", []))
        ids.add(" ".join([*n.get("given", []), n.get("family", "")]))
    for t in patient.get("telecom", []):
        ids.add(t["value"])
    return {i for i in ids if i and len(i) >= 3}


GENERIC_PATTERNS = [
    re.compile(r"\bMRN-\d+\b"),
    re.compile(r"\bLHP-\d+\b"),
    re.compile(r"\+?\d{2}-\d{5}-\d{5}"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
]


class PHILeak(RuntimeError):
    pass


def find_leaks(payload: object, denylist: Iterable[str]) -> list[str]:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    hits = [d for d in denylist if re.search(rf"\b{re.escape(d)}\b", text)]
    hits += [m.group(0) for p in GENERIC_PATTERNS for m in p.finditer(text)]
    return sorted(set(hits))


def assert_no_phi(payload: object, denylist: Iterable[str], where: str) -> None:
    leaks = find_leaks(payload, denylist)
    if leaks:
        # Never echo the leaked values themselves into logs or exceptions.
        raise PHILeak(f"{len(leaks)} direct identifier(s) detected in {where}; payload blocked")
