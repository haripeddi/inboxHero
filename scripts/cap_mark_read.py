#!/usr/bin/env python3
"""X1 — Mark daily/weekly digests as read (CrewAI + Ollama Llama)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crew_agents import run_crew_mark_read, write_output  # noqa: E402


def main() -> None:
    result = run_crew_mark_read()
    path = write_output("mark_read.json", result)
    print(f"Wrote {path}")
    print(f"framework: {result.get('framework')}")
    print(f"llm: {result.get('llm')}")
    print(f"observable: {result.get('observable')}")
    for c in result.get("candidates") or []:
        print(f"  marked {c['message_id']}: {c['subject'][:70]}")


if __name__ == "__main__":
    main()
