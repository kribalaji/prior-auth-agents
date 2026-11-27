# ADR 0004: Check for PHI at every boundary, and fail closed

**Status:** accepted

## Context
De-identification at the source can be bypassed later, for example by an LLM output that repeats a name it inferred, or by a future code change.

## Decision
Check payloads at each boundary: LLM prompt, LLM output, outbound A2A request, and inbound on the payer side. Each check combines a per-patient denylist of direct identifiers with generic patterns (MRN, member ID, phone, email). A detection blocks the step, and the error message never contains the detected value. A test sends every scenario through the system and asserts that no identifier appears in any prompt or A2A request.

## Consequences
- Defence in depth: one missed control does not cause a disclosure.
- Pattern checks can produce false positives, which block a request; that is the intended failure mode.
