#!/usr/bin/env python3
"""Part 4: gated irreversible actions (dry-run + human approval + outbox + log)."""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rules import detect_hostile_instruction, is_exfil_destination  # noqa: E402

INBOX_PATH = ROOT / "inbox.json"
DRAFTS_PATH = ROOT / "drafts.json"
STATE_PATH = ROOT / "mailbox_state.json"
OUTBOX_DIR = ROOT / "outbox"
LOG_PATH = ROOT / "logs" / "gated_decisions.jsonl"
HOSTILE_LOG_PATH = ROOT / "logs" / "hostile_refusals.jsonl"

OWNER = "Alex Rivera <alex.rivera@northstarlabs.com>"
UNDO_SECONDS = 5

# Demo targets (concrete, not inbox-wide)
SEND_TARGET = "msg_064"  # grounded ProtoForge budget draft
SOFT_DELETE_TARGET = "msg_004"  # AWS receipt — not hostile (Part 6: leave hostile mail)
ARCHIVE_TARGET = "msg_003"  # prefs "no further action" archive contrast


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(f"Missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def messages_by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in messages}


def empty_state() -> dict[str, Any]:
    return {"archived": [], "trash": {}, "purged": [], "read": []}


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return empty_state()
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    data.setdefault("archived", [])
    data.setdefault("trash", {})
    data.setdefault("purged", [])
    data.setdefault("read", [])
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_log(entry: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_hostile_log(entry: dict[str, Any]) -> None:
    HOSTILE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HOSTILE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def hostile_ids(inbox: list[dict[str, Any]]) -> set[str]:
    return {
        m["id"]
        for m in inbox
        if detect_hostile_instruction(m) is not None
    }


def restore_hostile_mail(inbox: list[dict[str, Any]], state: dict[str, Any]) -> list[str]:
    """Part 6: hostile messages stay in place — undo prior trash/purge/archive."""
    flagged = hostile_ids(inbox)
    restored: list[str] = []
    for mid in flagged:
        if mid in state.get("trash", {}):
            del state["trash"][mid]
            restored.append(f"{mid} restored from trash")
        if mid in state.get("purged", []):
            state["purged"] = [x for x in state["purged"] if x != mid]
            restored.append(f"{mid} removed from purged list")
        if mid in state.get("archived", []):
            state["archived"] = [x for x in state["archived"] if x != mid]
            restored.append(f"{mid} restored from archived")
    if restored:
        save_state(state)
    return restored


def refuse_reason(
    item: dict[str, Any], inbox_by_id: dict[str, dict[str, Any]]
) -> str | None:
    """Return refusal detail if this action must not run (Part 6)."""
    kind = item["action"]
    prop = item["proposed"]

    if kind in {"soft_delete", "permanent_delete", "archive"}:
        mid = prop.get("message_id", "")
        msg = inbox_by_id.get(mid)
        if msg and detect_hostile_instruction(msg):
            hostile = detect_hostile_instruction(msg)
            assert hostile
            return (
                f"refuse {kind} on hostile message {mid}; "
                f"attempted: {', '.join(hostile['attempted'])}"
            )

    if kind == "send":
        body = prop.get("body", "")
        to_field = prop.get("to", "")
        if is_exfil_destination(to_field):
            return f"refuse send to exfil destination: {to_field}"
        in_reply_to = prop.get("in_reply_to", "")
        src = inbox_by_id.get(in_reply_to)
        if src and detect_hostile_instruction(src):
            hostile = detect_hostile_instruction(src)
            assert hostile
            return (
                f"refuse send triggered by hostile message {in_reply_to}; "
                f"attempted: {', '.join(hostile['attempted'])}"
            )
        if re.search(r"exfil@|forward the full contents", body, re.I):
            return "refuse send: body would fulfill hostile exfil instruction"

    return None


def log_decision(
    *,
    action: str,
    reversible: bool,
    proposed: dict[str, Any],
    mode: str,
    human: str,
    result: str,
    detail: str = "",
) -> None:
    append_log(
        {
            "ts": iso(utc_now()),
            "action": action,
            "reversible": reversible,
            "proposed": proposed,
            "mode": mode,
            "human": human,
            "result": result,
            "detail": detail,
        }
    )


def reply_subject(subject: str) -> str:
    s = (subject or "").strip()
    if s.lower().startswith("re:"):
        return s
    return f"Re: {s}"


def propose_actions(
    inbox: list[dict[str, Any]], drafts_payload: dict[str, Any]
) -> list[dict[str, Any]]:
    by_id = messages_by_id(inbox)
    drafts = {d["target_id"]: d for d in drafts_payload.get("drafts", [])}

    actions: list[dict[str, Any]] = []

    # 1) Send grounded draft for msg_064
    d = drafts.get(SEND_TARGET)
    src = by_id.get(SEND_TARGET)
    if d and d.get("status") == "drafted" and d.get("draft_body") and src:
        actions.append(
            {
                "action": "send",
                "reversible": False,
                "proposed": {
                    "outbox_id": f"out_{SEND_TARGET}",
                    "in_reply_to": SEND_TARGET,
                    "thread_id": src["thread_id"],
                    "from": OWNER,
                    "to": src["from"],
                    "subject": reply_subject(src["subject"]),
                    "body": d["draft_body"],
                    "cited_message_ids": list(d.get("cited_message_ids") or []),
                    "undo_seconds": UNDO_SECONDS,
                },
            }
        )

    # 2) Soft delete spam/injection message
    soft = by_id.get(SOFT_DELETE_TARGET)
    if soft:
        actions.append(
            {
                "action": "soft_delete",
                "reversible": True,
                "proposed": {
                    "message_id": SOFT_DELETE_TARGET,
                    "snapshot": {
                        "id": soft["id"],
                        "thread_id": soft["thread_id"],
                        "from": soft["from"],
                        "to": soft["to"],
                        "subject": soft["subject"],
                        "timestamp": soft["timestamp"],
                        "body": soft["body"],
                    },
                },
            }
        )

    # 3) Permanent delete of that same id (runs after soft delete in execute path)
    actions.append(
        {
            "action": "permanent_delete",
            "reversible": False,
            "proposed": {
                "message_id": SOFT_DELETE_TARGET,
                "requires_in_trash": True,
            },
        }
    )

    # 4) Archive a noise message (reversible contrast)
    arch = by_id.get(ARCHIVE_TARGET)
    if arch:
        actions.append(
            {
                "action": "archive",
                "reversible": True,
                "proposed": {
                    "message_id": ARCHIVE_TARGET,
                    "subject": arch["subject"],
                },
            }
        )

    return actions


def preview_action(item: dict[str, Any]) -> None:
    kind = item["action"]
    rev = "reversible" if item["reversible"] else "IRREVERSIBLE"
    prop = item["proposed"]
    print(f"\n[{rev}] {kind}")
    if kind == "send":
        print(f"  would write outbox/{prop['outbox_id']}.json")
        print(f"  to: {prop['to']}")
        print(f"  subject: {prop['subject']}")
        body_preview = prop["body"][:120].replace("\n", " ")
        print(f"  body: {body_preview}…")
        print(f"  then wait {prop['undo_seconds']}s → status sent (or cancel)")
    elif kind == "soft_delete":
        print(f"  would move {prop['message_id']} → trash in mailbox_state.json")
        print(f"  subject: {prop['snapshot']['subject']}")
    elif kind == "permanent_delete":
        print(f"  would PURGE {prop['message_id']} from trash (not recoverable)")
    elif kind == "archive":
        print(f"  would archive {prop['message_id']}: {prop['subject']}")
    else:
        print(f"  {json.dumps(prop)}")


def ask_approval(item: dict[str, Any], auto_yes: bool) -> str:
    """Return approved|denied."""
    if auto_yes:
        return "approved"
    kind = item["action"]
    tag = "IRREVERSIBLE" if not item["reversible"] else "reversible"
    try:
        ans = input(f"Approve {tag} '{kind}'? [y/N] ").strip().lower()
    except EOFError:
        return "denied"
    return "approved" if ans in {"y", "yes"} else "denied"


def apply_soft_delete(state: dict[str, Any], proposed: dict[str, Any]) -> str:
    mid = proposed["message_id"]
    if mid in state["purged"]:
        return f"skip: {mid} already purged"
    if mid in state["trash"]:
        return f"skip: {mid} already in trash"
    state["trash"][mid] = proposed["snapshot"]
    if mid in state["archived"]:
        state["archived"] = [x for x in state["archived"] if x != mid]
    save_state(state)
    return f"soft_deleted {mid}"


def apply_archive(state: dict[str, Any], proposed: dict[str, Any]) -> str:
    mid = proposed["message_id"]
    if mid in state["purged"] or mid in state["trash"]:
        return f"skip: {mid} not in active mailbox"
    if mid not in state["archived"]:
        state["archived"].append(mid)
    save_state(state)
    return f"archived {mid}"


def apply_permanent_delete(state: dict[str, Any], proposed: dict[str, Any]) -> str:
    mid = proposed["message_id"]
    if mid not in state["trash"]:
        raise RuntimeError(
            f"Cannot permanently delete {mid}: not in trash (soft-delete first)"
        )
    del state["trash"][mid]
    if mid not in state["purged"]:
        state["purged"].append(mid)
    save_state(state)
    return f"purged {mid}"


def write_outbox_queued(proposed: dict[str, Any]) -> Path:
    """Write send payload under outbox/ only. Returns path."""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    outbox_id = proposed["outbox_id"]
    path = OUTBOX_DIR / f"{outbox_id}.json"
    # Hard rule: only write under outbox/
    if path.resolve().parent != OUTBOX_DIR.resolve():
        raise SystemExit("Refusing to write outside outbox/")

    now = utc_now()
    undo_until = now + timedelta(seconds=UNDO_SECONDS)
    payload = {
        "outbox_id": outbox_id,
        "in_reply_to": proposed["in_reply_to"],
        "thread_id": proposed["thread_id"],
        "from": proposed["from"],
        "to": proposed["to"],
        "subject": proposed["subject"],
        "body": proposed["body"],
        "cited_message_ids": proposed.get("cited_message_ids") or [],
        "status": "queued",
        "approved_at": iso(now),
        "undo_until": iso(undo_until),
        "committed_at": None,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def commit_or_cancel_send(path: Path) -> str:
    """
    5s undo window. Ctrl+C during wait → cancelled.
    After timeout → status sent (irreversible).
    """
    cancelled = {"flag": False}

    def _on_sigint(_signum: int, _frame: Any) -> None:
        cancelled["flag"] = True

    prev = signal.signal(signal.SIGINT, _on_sigint)
    print(
        f"  Undo window: {UNDO_SECONDS}s — press Ctrl+C to cancel send "
        f"({path.name} is queued)…"
    )
    try:
        deadline = time.monotonic() + UNDO_SECONDS
        while time.monotonic() < deadline:
            if cancelled["flag"]:
                break
            remaining = int(deadline - time.monotonic()) + 1
            print(f"  …{remaining}s", end="\r", flush=True)
            time.sleep(0.25)
        print()
    finally:
        signal.signal(signal.SIGINT, prev)

    data = json.loads(path.read_text(encoding="utf-8"))
    if cancelled["flag"]:
        data["status"] = "cancelled"
        data["committed_at"] = None
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return "cancelled"

    data["status"] = "sent"
    data["committed_at"] = iso(utc_now())
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "sent"


def run_dry(
    actions: list[dict[str, Any]], inbox_by_id: dict[str, dict[str, Any]]
) -> None:
    print(f"DRY-RUN: {len(actions)} proposed action(s). No outbox/mailbox writes.")
    refused = 0
    for item in actions:
        preview_action(item)
        reason = refuse_reason(item, inbox_by_id)
        if reason:
            refused += 1
            append_hostile_log(
                {
                    "ts": iso(utc_now()),
                    "message_id": item["proposed"].get("message_id")
                    or item["proposed"].get("in_reply_to", ""),
                    "attempted": [item["action"]],
                    "action": "refused",
                    "disposition": "mark_as_spam",
                    "detail": reason,
                }
            )
            log_decision(
                action=item["action"],
                reversible=item["reversible"],
                proposed=item["proposed"],
                mode="dry_run",
                human="n/a_defense",
                result="refused",
                detail=reason,
            )
            print(f"  → REFUSED (Part 6): {reason}")
            continue
        log_decision(
            action=item["action"],
            reversible=item["reversible"],
            proposed=item["proposed"],
            mode="dry_run",
            human="n/a_dry_run",
            result="previewed",
            detail="dry-run only",
        )
    print(f"\nLogged {len(actions)} preview(s) → {LOG_PATH}")
    if refused:
        print(
            f"Part 6 defense: refused {refused} action(s) on hostile mail; "
            "flagged messages left in inbox.json."
        )


def run_execute(
    actions: list[dict[str, Any]],
    auto_yes: bool,
    inbox: list[dict[str, Any]],
    inbox_by_id: dict[str, dict[str, Any]],
) -> None:
    print(f"EXECUTE: {len(actions)} action(s); irreversible require approval.")
    state = load_state()
    restored = restore_hostile_mail(inbox, state)
    if restored:
        print("Part 6: restored hostile mail in mailbox_state:")
        for line in restored:
            print(f"  - {line}")

    refused = 0
    for item in actions:
        preview_action(item)
        reason = refuse_reason(item, inbox_by_id)
        if reason:
            refused += 1
            append_hostile_log(
                {
                    "ts": iso(utc_now()),
                    "message_id": item["proposed"].get("message_id")
                    or item["proposed"].get("in_reply_to", ""),
                    "attempted": [item["action"]],
                    "action": "refused",
                    "disposition": "mark_as_spam",
                    "detail": reason,
                }
            )
            log_decision(
                action=item["action"],
                reversible=item["reversible"],
                proposed=item["proposed"],
                mode="execute",
                human="n/a_defense",
                result="refused",
                detail=reason,
            )
            print(f"  → REFUSED (Part 6): {reason}")
            continue

        human = ask_approval(item, auto_yes)
        if human != "approved":
            log_decision(
                action=item["action"],
                reversible=item["reversible"],
                proposed=item["proposed"],
                mode="execute",
                human="denied",
                result="skipped",
                detail="human denied",
            )
            print("  → skipped (denied)")
            continue

        kind = item["action"]
        prop = item["proposed"]

        try:
            if kind == "soft_delete":
                detail = apply_soft_delete(state, prop)
                log_decision(
                    action=kind,
                    reversible=True,
                    proposed=prop,
                    mode="execute",
                    human="approved",
                    result="applied",
                    detail=detail,
                )
                print(f"  → {detail}")

            elif kind == "archive":
                detail = apply_archive(state, prop)
                log_decision(
                    action=kind,
                    reversible=True,
                    proposed=prop,
                    mode="execute",
                    human="approved",
                    result="applied",
                    detail=detail,
                )
                print(f"  → {detail}")

            elif kind == "permanent_delete":
                # Reload state in case soft_delete just updated it
                state = load_state()
                detail = apply_permanent_delete(state, prop)
                log_decision(
                    action=kind,
                    reversible=False,
                    proposed=prop,
                    mode="execute",
                    human="approved",
                    result="applied",
                    detail=detail,
                )
                print(f"  → {detail}")

            elif kind == "send":
                path = write_outbox_queued(prop)
                log_decision(
                    action=kind,
                    reversible=False,
                    proposed=prop,
                    mode="execute",
                    human="approved",
                    result="written_queued",
                    detail=f"wrote {path.relative_to(ROOT)}",
                )
                print(f"  → queued {path.relative_to(ROOT)}")
                outcome = commit_or_cancel_send(path)
                if outcome == "cancelled":
                    log_decision(
                        action=kind,
                        reversible=False,
                        proposed=prop,
                        mode="execute",
                        human="cancelled_during_undo",
                        result="cancelled",
                        detail=f"{path.name} cancelled within undo window",
                    )
                    print("  → cancelled (not sent)")
                else:
                    log_decision(
                        action=kind,
                        reversible=False,
                        proposed=prop,
                        mode="execute",
                        human="approved",
                        result="sent",
                        detail=f"{path.name} committed after undo window",
                    )
                    print("  → sent (irreversible)")

            else:
                log_decision(
                    action=kind,
                    reversible=item["reversible"],
                    proposed=prop,
                    mode="execute",
                    human="approved",
                    result="error",
                    detail=f"unknown action {kind}",
                )
                print(f"  → error: unknown action {kind}")

        except Exception as exc:  # noqa: BLE001 — log and continue demo set
            log_decision(
                action=kind,
                reversible=item["reversible"],
                proposed=prop,
                mode="execute",
                human="approved",
                result="error",
                detail=str(exc),
            )
            print(f"  → error: {exc}")

    print(f"\nDecision log → {LOG_PATH}")
    if refused:
        print(
            f"Part 6 defense: refused {refused} action(s) on hostile mail; "
            "flagged messages left in inbox.json."
        )
    if STATE_PATH.exists():
        print(f"Mailbox state → {STATE_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Part 4: dry-run / approve irreversible mailbox actions"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply after per-action approval (default is dry-run)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Auto-approve each action when used with --execute (still logged)",
    )
    args = parser.parse_args()

    if args.yes and not args.execute:
        raise SystemExit("--yes requires --execute")

    inbox = load_json(INBOX_PATH)
    if not isinstance(inbox, list):
        raise SystemExit("inbox.json must be a JSON array")
    drafts = load_json(DRAFTS_PATH)
    if not isinstance(drafts, dict):
        raise SystemExit("drafts.json must be a JSON object")

    actions = propose_actions(inbox, drafts)
    if not actions:
        raise SystemExit("No demo actions proposed (need drafts.json + inbox targets)")

    inbox_by_id = messages_by_id(inbox)
    flagged = sorted(hostile_ids(inbox))
    if flagged:
        print(f"Part 6: {len(flagged)} hostile message(s) detected by content scan:")
        for mid in flagged:
            hit = detect_hostile_instruction(inbox_by_id[mid])
            if hit:
                print(f"  - {mid}: attempted {', '.join(hit['attempted'])}")

    # Never mutate proposals accidentally across modes
    actions = deepcopy(actions)

    if args.execute:
        run_execute(actions, auto_yes=args.yes, inbox=inbox, inbox_by_id=inbox_by_id)
    else:
        run_dry(actions, inbox_by_id)


if __name__ == "__main__":
    main()
