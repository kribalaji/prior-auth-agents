"""Payer policy MCP server. Serves machine-readable coverage criteria.

Run standalone:  python -m priorauth.mcp_policy   (stdio transport)
"""

from __future__ import annotations

from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from priorauth.rules import PolicyBook, leaf_criteria

DEFAULT_POLICIES = Path(__file__).resolve().parents[2] / "policies" / "policies.yaml"


class PolicySummary(BaseModel):
    id: str
    title: str


class PolicyList(BaseModel):
    policies: list[PolicySummary]


class EvidenceNeed(BaseModel):
    criterion_id: str
    type: str
    text: str = ""
    bypass: bool = False
    params: dict


class PolicyRequirements(BaseModel):
    policy_id: str
    title: str
    criteria_text: list[str]
    evidence_needed: list[EvidenceNeed]


def build_server(book: PolicyBook | None = None) -> MCPServer:
    book = book or PolicyBook.load(DEFAULT_POLICIES)
    mcp = MCPServer("payer-policy")

    @mcp.tool()
    def list_policies() -> PolicyList:
        """Services that require prior authorization."""
        return PolicyList(policies=[PolicySummary(id=p["id"], title=p["title"]) for p in book.spec["policies"]])

    @mcp.tool()
    def get_requirements(service_code: str) -> PolicyRequirements:
        """Criteria for a service and the exact evidence items a provider should gather."""
        p = book.get(service_code)
        if p is None:
            raise ToolError(f"No prior-authorization policy for {service_code}")
        needs = []
        tops = [(c, True) for c in p.get("bypass", [])] + [(c, False) for c in p["criteria"]]
        for top, is_bypass in tops:
            for leaf in leaf_criteria(top):
                params = {k: v for k, v in leaf.items() if k not in ("id", "type", "text")}
                needs.append(EvidenceNeed(criterion_id=top["id"], type=leaf["type"], text=top["text"],
                                          bypass=is_bypass, params=params))
        return PolicyRequirements(policy_id=p["id"], title=p["title"],
                                  criteria_text=[c["text"] for c in p["criteria"]], evidence_needed=needs)

    return mcp


if __name__ == "__main__":
    build_server().run()
