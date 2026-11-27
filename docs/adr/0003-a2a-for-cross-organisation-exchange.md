# ADR 0003: Use A2A between organisations, MCP within an organisation

**Status:** accepted

## Context
The provider and the payer are separate organisations, each running its own agent, models and data. MCP connects an agent to tools and data it controls. It does not model a peer agent with its own task lifecycle.

## Decision
The payer publishes an **A2A 1.0 Agent Card** and a JSON-RPC endpoint. A request is an A2A Task. Missing evidence maps to `INPUT_REQUIRED`, and the provider answers in the same task with more evidence. Inside each organisation, agents use MCP to reach tools: FHIR on the provider side, policy on the payer side.

## Consequences
- Each side can change models or frameworks without breaking the other.
- Multi-turn negotiation ("send the HbA1c result") is native to the protocol.
- Production still needs agent authentication and signed cards (see the README limitations).
