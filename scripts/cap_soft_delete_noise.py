#!/usr/bin/env python3
"""X2 — Soft-delete phishing / spam / ads (CrewAI); never delete hostile mail."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crew_agents import run_crew_soft_delete, write_output  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Apply soft-deletes to mailbox_state trash (default: dry-run)",
    )
    args = ap.parse_args()

    result = run_crew_soft_delete(execute=args.execute)
    path = write_output("soft_delete_batch.json", result)
    print(f"Wrote {path}")
    print(f"framework: {result.get('framework')}")
    print(f"llm: {result.get('llm')}")
    print(f"observable: {result.get('observable')}")
    for r in result.get("refused_hostile") or []:
        print(f"  REFUSED {r['message_id']}: {r.get('reason')}")
    for r in result.get("results") or []:
        print(f"  {r.get('status')}: {r.get('message_id')}")


if __name__ == "__main__":
    main()
