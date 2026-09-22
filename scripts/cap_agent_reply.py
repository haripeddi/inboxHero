#!/usr/bin/env python3
"""X3 — CrewAI agentic reply using email + Slack + calendar + prefs (Ollama Llama)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crew_agents import run_crew_agent_reply, write_output  # noqa: E402

# Default: Maya board-risk note (VIP simple guidance → auto path candidate)
DEFAULT_VIP_ID = "msg_085"
# Hold demo: ProtoForge approve-as-discussed (spend → never auto)
DEFAULT_HOLD_ID = "msg_064"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--id",
        default=DEFAULT_VIP_ID,
        help=f"Target message id (default VIP demo {DEFAULT_VIP_ID})",
    )
    ap.add_argument(
        "--hold-demo",
        action="store_true",
        help=f"Use spend ask {DEFAULT_HOLD_ID} to demonstrate hold_for_human",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Stage outbox/ when decision is auto_stage_outbox",
    )
    args = ap.parse_args()
    mid = DEFAULT_HOLD_ID if args.hold_demo else args.id

    result = run_crew_agent_reply(mid, execute=args.execute)
    path = write_output("agent_reply.json", result)
    print(f"Wrote {path}")
    print(f"framework: {result.get('framework')}")
    print(f"target: {result.get('target_id')}  decision: {result.get('decision')}")
    print(f"llm: {result.get('llm')}")
    print(f"cited_email: {result.get('cited_email_ids')}")
    print(f"cited_slack: {result.get('cited_slack_ids')}")
    print(f"cited_calendar: {result.get('cited_calendar_ids')}")
    print(f"outbox: {result.get('outbox')}")
    body = (result.get("draft_body") or "")[:400]
    print("--- draft ---")
    print(body)


if __name__ == "__main__":
    main()
