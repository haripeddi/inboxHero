#!/usr/bin/env python3
"""Zero the inbox: assign exactly one disposition to every message."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Allow `python3 scripts/zero_inbox.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from judge import judge_message  # noqa: E402
from rules import detect_hostile_instruction, try_rule  # noqa: E402

INBOX_PATH = ROOT / "inbox.json"
OUT_PATH = ROOT / "dispositions.json"
HOSTILE_LOG_PATH = ROOT / "logs" / "hostile_refusals.jsonl"
VOCAB = frozenset(
    {
        "reply",
        "reply_all",
        "archive",
        "defer",
        "delegate",
        "escalate",
        "mark_as_spam",
        "move_to_folder",
    }
)


def _load_prefs_text(messages: list[dict]) -> str:
    prefs = [m for m in messages if m.get("thread_id") == "thr_prefs"]
    prefs.sort(key=lambda m: m["timestamp"])
    return "\n\n".join(f"{m['from']}:\n{m['body']}" for m in prefs)


def zero_inbox(messages: list[dict]) -> dict:
    by_thread: dict[str, list[dict]] = {}
    for m in messages:
        by_thread.setdefault(m["thread_id"], []).append(m)
    for tid in by_thread:
        by_thread[tid].sort(key=lambda m: (m["timestamp"], m["id"]))

    prefs_text = _load_prefs_text(messages)
    dispositions: list[dict] = []
    rules_only = 0
    model_count = 0

    for msg in messages:
        hit = try_rule(msg)
        if hit:
            route = "rules"
            rules_only += 1
            row = {
                "id": msg["id"],
                "disposition": hit["disposition"],
                "reason": hit["reason"],
                "route": route,
            }
        else:
            judged = judge_message(msg, by_thread[msg["thread_id"]], prefs_text)
            model_count += 1
            row = {
                "id": msg["id"],
                "disposition": judged["disposition"],
                "reason": judged["reason"],
                "route": "model",
            }
        dispositions.append(row)

    # Coverage gate — first check when the run finishes
    inbox_ids = {m["id"] for m in messages}
    disp_ids = {d["id"] for d in dispositions}
    missing = sorted(inbox_ids - disp_ids)
    extra = sorted(disp_ids - inbox_ids)
    if missing or extra or len(dispositions) != len(messages):
        raise SystemExit(
            f"Coverage failed: missing={missing} extra={extra} "
            f"len_disp={len(dispositions)} len_inbox={len(messages)}"
        )

    for d in dispositions:
        if d["disposition"] not in VOCAB:
            raise SystemExit(f"Invalid disposition for {d['id']}: {d['disposition']}")
        if not d.get("reason"):
            raise SystemExit(f"Empty reason for {d['id']}")
        if d["route"] not in {"rules", "model"}:
            raise SystemExit(f"Invalid route for {d['id']}: {d['route']}")

    return {
        "message_count": len(messages),
        "rules_only_count": rules_only,
        "model_count": model_count,
        "dispositions": dispositions,
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_and_refuse_hostile(messages: list[dict]) -> list[dict]:
    """Detect hostile assistant instructions; log refusals; leave messages in place."""
    findings: list[dict] = []
    HOSTILE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    for msg in messages:
        hit = detect_hostile_instruction(msg)
        if not hit:
            continue
        entry = {
            "ts": _iso_now(),
            "message_id": hit["message_id"],
            "attempted": hit["attempted"],
            "action": "refused",
            "disposition": "mark_as_spam",
            "detail": hit["evidence"],
        }
        findings.append(entry)
        with HOSTILE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return findings


def print_hostile_summary(findings: list[dict], messages: list[dict]) -> None:
    print("\n=== HOSTILE INBOX ===")
    if not findings:
        print("No assistant-directed hostile instructions detected.")
        return

    print(f"Found {len(findings)} message(s) with instructions addressed to an assistant:")
    for entry in findings:
        asks = ", ".join(entry["attempted"])
        print(f"  - {entry['message_id']}: attempted {asks}")
        print(f"    evidence: {entry['detail']}")

    flagged_ids = {e["message_id"] for e in findings}
    still_present = flagged_ids.issubset({m["id"] for m in messages})
    print(
        "\nRefused all hostile asks — nothing sent to outbox/, nothing deleted. "
        "Messages flagged mark_as_spam and left in inbox.json."
    )
    print(f"Still present in inbox.json: {still_present}")
    print(f"Refusal log → {HOSTILE_LOG_PATH}")


def main() -> None:
    messages = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        raise SystemExit("inbox.json must be a JSON array")

    result = zero_inbox(messages)
    OUT_PATH.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    hostile_findings = scan_and_refuse_hostile(messages)
    print_hostile_summary(hostile_findings, messages)

    hist = Counter(d["disposition"] for d in result["dispositions"])
    print(f"Wrote {OUT_PATH}")
    print(f"messages: {result['message_count']}")
    print(
        f"never required a model call (rules): {result['rules_only_count']}"
    )
    print(f"required model path: {result['model_count']}")
    print("disposition histogram:")
    for name, count in sorted(hist.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
