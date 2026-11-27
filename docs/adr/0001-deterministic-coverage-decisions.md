# ADR 0001: Coverage is decided by a rules engine; an unmet criterion goes to a human

**Status:** accepted

## Context
Coverage decisions affect patient care and must be explainable, reproducible and auditable. LLM judgements are none of these by default. Regulators also expect a specific reason for every denial.

## Decision
`rules.py` evaluates machine-readable criteria and returns `met`, `not_met` or `missing_evidence` for each one, with evidence references. The outcomes are approve, pend for information, or **route to clinical review**. There is no automatic denial. LLMs only draft text (the justification and the member letter) from structured inputs.

## Consequences
- The same packet always gets the same outcome, and each outcome cites its criteria and evidence.
- Policy changes are reviewed YAML diffs.
- Clinicians spend their time only on the cases that need judgement.
