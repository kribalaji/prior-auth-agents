"""Command line.

  priorauth demo                       run every synthetic scenario end to end, in one process
  priorauth submit P001 GLP1-T2D       run one request and print the trace, determination and letter
  priorauth serve-payer --port 9100    run the payer A2A agent over HTTP
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import yaml

from priorauth.llm import from_env
from priorauth.provider_agent import in_memory_agent

ROOT = Path(__file__).resolve().parents[2]


async def _demo() -> int:
    agent, app = in_memory_agent(llm=from_env())
    cases = yaml.safe_load((ROOT / "evals/scenarios.yaml").read_text())
    print(f"{'patient':8} {'service':12} {'A2A state':15} {'outcome':27} expected")
    ok = 0
    for c in cases:
        s = await agent.run(c["patient"], c["service"])
        outcome = s["determination"]["outcome"]
        ok += outcome == c["outcome"]
        mark = "✓" if outcome == c["outcome"] else "✗"
        print(f"{c['patient']:8} {c['service']:12} {s['task_state']:15} {outcome:27} {mark}")
    print(f"\n{ok}/{len(cases)} as expected; {len(app.state.service.review_queue)} case(s) in the clinical review queue")
    return 0 if ok == len(cases) else 1


async def _submit(patient: str, service: str) -> int:
    agent, _ = in_memory_agent(llm=from_env())
    s = await agent.run(patient, service)
    if s.get("error"):
        print("error:", s["error"])
        return 1
    print("trace:\n  " + "\n  ".join(s["trace"]))
    print("\njustification sent to payer:\n" + s["justification"])
    print("\ndetermination:\n" + json.dumps(s["determination"], indent=2))
    print("\nletter:\n" + s["letter"])
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("a2a").setLevel(logging.ERROR)  # SDK logs a benign warning on in-process shutdown
    ap = argparse.ArgumentParser(prog="priorauth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    sp = sub.add_parser("submit")
    sp.add_argument("patient")
    sp.add_argument("service")
    pp = sub.add_parser("serve-payer")
    pp.add_argument("--port", type=int, default=9100)
    args = ap.parse_args()
    if args.cmd == "serve-payer":
        import uvicorn

        from priorauth.payer_agent import build_app
        uvicorn.run(build_app(base_url=f"http://localhost:{args.port}"), port=args.port)
        return
    code = asyncio.run(_demo() if args.cmd == "demo" else _submit(args.patient, args.service))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
