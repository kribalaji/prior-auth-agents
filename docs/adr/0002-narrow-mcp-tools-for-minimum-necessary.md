# ADR 0002: Expose narrow, criterion-shaped MCP tools instead of a generic FHIR query

**Status:** accepted

## Context
Giving an agent a general "search FHIR" tool lets it read anything in the record. That conflicts with the HIPAA minimum-necessary principle, and it makes prompt-injection impact larger.

## Decision
The FHIR MCP server offers `patient_context`, `find_conditions`, `find_observations`, `find_medications`, `find_procedures` and `find_allergies`. Each takes the specific code and time window that a policy criterion names. Outputs are already de-identified: keyed pseudonymous references, age instead of birth date, and day offsets instead of dates.

## Consequences
- The agent can only fetch what policy requirements ask for, and every call is auditable.
- New criterion types need a new tool, which is a deliberate, reviewable change.
