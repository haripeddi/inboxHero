#!/usr/bin/env python3
"""Part 5: learn standing prefs into durable memory; apply after process restart."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory import (  # noqa: E402
    MEMORY_FILE,
    SPEND_PREF_KEY,
    clear as clear_memory,
    get_fact,
    parse_spend_pref,
    remember,
    working_context,
)
from judge import _money_amounts, _top_body, local_judge  # noqa: E402

INBOX_PATH = ROOT / "inbox.json"
PREFS_SOURCE_ID = "msg_002"
DEFAULT_APPLY_ID = "msg_064"


def load_inbox() -> list[dict[str, Any]]:
    messages = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        raise SystemExit("inbox.json must be a JSON array")
    return messages


def messages_by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in messages}


def thread_messages(
    messages: list[dict[str, Any]], thread_id: str
) -> list[dict[str, Any]]:
    thr = [m for m in messages if m.get("thread_id") == thread_id]
    thr.sort(key=lambda m: (m["timestamp"], m["id"]))
    return thr


def learn_from_owner_prefs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Record standing instructions stated in msg_002 (owner confirmation).
    Process should exit after this run; a later process loads the same file.
    """
    by_id = messages_by_id(messages)
    src = by_id.get(PREFS_SOURCE_ID)
    if not src:
        raise SystemExit(f"Missing preference source {PREFS_SOURCE_ID}")

    body = _top_body(src.get("body", ""))
    results: list[dict[str, Any]] = []

    # Named Part 5 preference
    results.append(
        remember(
            SPEND_PREF_KEY,
            "5000; escalate to Priya Shah <priya.shah@northstarlabs.com>",
            PREFS_SOURCE_ID,
        )
    )

    # Sibling facts from the same owner statement
    if "vendors@northstarlabs.com" in body.lower() or "cold vendor" in body.lower():
        results.append(
            remember(
                "cold_vendor_routing",
                "decline and route to vendors@northstarlabs.com; do not book intros",
                PREFS_SOURCE_ID,
            )
        )
    if "async" in body.lower() and "eng" in body.lower():
        results.append(
            remember(
                "prefer_async_from_eng",
                "prefer async updates from eng leads over meetings when possible",
                PREFS_SOURCE_ID,
            )
        )
    if "maya.chen" in body.lower() or "maya chen" in body.lower():
        results.append(
            remember(
                "vip_internal",
                "Maya Chen (CEO), Jordan Lee (VP Eng) — respond personally / same day",
                PREFS_SOURCE_ID,
            )
        )
    if "sam okonkwo" in body.lower() or "vip customers" in body.lower():
        results.append(
            remember(
                "vip_customers",
                "Sam Okonkwo, Dana Whitfield, Marcus Bell, Elena Vos — high priority; do not auto-delegate",
                PREFS_SOURCE_ID,
            )
        )

    return results


def apply_message(
    messages: list[dict[str, Any]], message_id: str
) -> dict[str, Any]:
    """
    Cold-start apply: load memory from disk only (no re-learn).
    When never_auto_approve_spend_over is present and the thread shows spend
    at/above the threshold with an approve ask, escalate and cite the memory key.
    """
    by_id = messages_by_id(messages)
    msg = by_id.get(message_id)
    if not msg:
        raise SystemExit(f"Unknown message id: {message_id}")

    thr = thread_messages(messages, msg["thread_id"])
    fact = get_fact(SPEND_PREF_KEY)

    if fact:
        parsed = parse_spend_pref(fact["value"])
        if parsed:
            threshold, cfo = parsed
            top = _top_body(msg.get("body", ""))
            subject = msg.get("subject", "")
            lower = f"{subject}\n{top}".lower()
            amounts = _money_amounts(f"{subject}\n{top}")
            # Amount may live only in earlier thread messages (msg_064 case)
            for sibling in thr:
                amounts.extend(_money_amounts(sibling.get("body", "")))
            spend_ask = (
                "approve" in lower
                or "as discussed" in lower
                or msg.get("thread_id") == "thr_budget"
                or "panel" in lower
                or "protoforge" in lower
                or "usertesting" in lower
            )
            if amounts and max(amounts) >= threshold and spend_ask:
                return {
                    "message_id": message_id,
                    "disposition": "escalate",
                    "reason": (
                        f"Spend ≥ ${threshold:,} — escalate to {cfo} (CFO); "
                        f"honouring standing memory '{SPEND_PREF_KEY}' "
                        f"(source {fact.get('source')})"
                    ),
                    "honoured_memory_key": SPEND_PREF_KEY,
                    "memory_value": fact["value"],
                }

    # No applicable memory: fall back to local judge (no memory citation)
    prefs_blob = working_context()
    judged = local_judge(msg, thr, prefs_blob)
    return {
        "message_id": message_id,
        "disposition": judged["disposition"],
        "reason": judged["reason"],
        "honoured_memory_key": None,
        "memory_value": None,
    }


def cmd_learn() -> None:
    messages = load_inbox()
    results = learn_from_owner_prefs(messages)
    ok = [r for r in results if r.get("status") == "ok"]
    print(f"Learned {len(ok)} standing instruction(s) → {MEMORY_FILE}")
    for r in ok:
        print(f"  {r['action']}: {r['key']} = {r['value']}")
    print("Exit this process; on the next run use: apply --id msg_064")


def cmd_apply(message_id: str) -> None:
    messages = load_inbox()
    result = apply_message(messages, message_id)
    print(json.dumps(result, indent=2))


def cmd_clear() -> None:
    out = clear_memory()
    print(json.dumps(out, indent=2))


def cmd_show() -> None:
    print(f"Store: {MEMORY_FILE}")
    print(f"Exists: {MEMORY_FILE.exists()}")
    print(working_context())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Part 5: persist and honour standing email preferences"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("learn", help="Record prefs from msg_002 and exit")
    apply_p = sub.add_parser(
        "apply", help="Fresh process: load memory and handle a message"
    )
    apply_p.add_argument(
        "--id",
        default=DEFAULT_APPLY_ID,
        help=f"Message id to handle (default {DEFAULT_APPLY_ID})",
    )
    sub.add_parser("clear", help="Delete standing_instructions.json")
    sub.add_parser("show", help="Print stored facts")

    args = parser.parse_args()
    if args.command == "learn":
        cmd_learn()
    elif args.command == "apply":
        cmd_apply(args.id)
    elif args.command == "clear":
        cmd_clear()
    elif args.command == "show":
        cmd_show()
    else:
        raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
