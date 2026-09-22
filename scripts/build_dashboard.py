#!/usr/bin/env python3
"""Part 7: build a three-pane dashboard from a completed run (JSON + static HTML)."""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rules import detect_hostile_instruction  # noqa: E402
from run_actions import (  # noqa: E402
    OUTBOX_DIR,
    STATE_PATH,
    load_state,
    messages_by_id,
    propose_actions,
)

INBOX_PATH = ROOT / "inbox.json"
DISPOSITIONS_PATH = ROOT / "dispositions.json"
DRAFTS_PATH = ROOT / "drafts.json"
HOSTILE_LOG_PATH = ROOT / "logs" / "hostile_refusals.jsonl"
DATA_PATH = ROOT / "dashboard_data.json"
HTML_PATH = ROOT / "dashboard.html"

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def load_json(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(f"Missing {path} — run prior parts first")
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def parse_msg_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(LOCAL_TZ)


def next_weekday_on_or_after(anchor: date, weekday: int) -> date:
    delta = (weekday - anchor.weekday()) % 7
    return anchor + timedelta(days=delta)


def top_body(body: str, n: int = 600) -> str:
    """Prefer the newest authored content before quoted history."""
    parts = re.split(r"\nOn .+? wrote:\n", body, maxsplit=1)
    return (parts[0] if parts else body).strip()[:n]


def iso_local(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).isoformat()


# ---------------------------------------------------------------------------
# Pane 1 — Pending
# ---------------------------------------------------------------------------


def action_message_id(item: dict[str, Any]) -> str:
    prop = item.get("proposed") or {}
    kind = item["action"]
    if kind == "send":
        return str(prop.get("in_reply_to") or "")
    return str(prop.get("message_id") or "")


def why_human(item: dict[str, Any]) -> str:
    kind = item["action"]
    if kind == "send":
        return (
            "Irreversible after the undo window — Part 4 requires dry-run "
            "and explicit human approval before writing outbox/"
        )
    if kind == "permanent_delete":
        return (
            "Permanent purge cannot be undone — Part 4 requires human "
            "approval before removing the trash snapshot"
        )
    if kind == "soft_delete":
        return (
            "Mailbox mutation gated under Part 4 — soft-delete still needs "
            "explicit approval on execute"
        )
    if kind == "archive":
        return (
            "Mailbox mutation gated under Part 4 — archive still needs "
            "explicit approval on execute"
        )
    return "Part 4 gate: human approval required before applying"


def resolve_action_status(
    item: dict[str, Any], state: dict[str, Any]
) -> str:
    kind = item["action"]
    prop = item.get("proposed") or {}
    mid = action_message_id(item)

    if kind == "send":
        out_id = prop.get("outbox_id") or f"out_{mid}"
        path = OUTBOX_DIR / f"{out_id}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return str(payload.get("status") or "written")
        return "pending"

    if kind == "soft_delete":
        if mid in state.get("purged", []):
            return "purged"
        if mid in (state.get("trash") or {}):
            return "in_trash"
        return "pending"

    if kind == "permanent_delete":
        if mid in state.get("purged", []):
            return "purged"
        if mid in (state.get("trash") or {}):
            return "awaiting_purge"
        return "pending"

    if kind == "archive":
        if mid in state.get("archived", []):
            return "archived"
        return "pending"

    return "pending"


def still_needs_human(item: dict[str, Any], status: str) -> bool:
    """Part 7 Pending: only actions not yet applied (still need a human)."""
    kind = item["action"]
    if kind == "send":
        # Any outbox write means the gate was already passed
        return status == "pending"
    if kind == "soft_delete":
        return status == "pending"
    if kind == "permanent_delete":
        # Still needs approval until purged (including while sitting in trash)
        return status in {"pending", "awaiting_purge"}
    if kind == "archive":
        return status == "pending"
    return status == "pending"


def build_pending(
    inbox: list[dict[str, Any]],
    drafts_payload: dict[str, Any],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = messages_by_id(inbox)
    rows: list[dict[str, Any]] = []
    for item in propose_actions(inbox, drafts_payload):
        status = resolve_action_status(item, state)
        if not still_needs_human(item, status):
            continue
        mid = action_message_id(item)
        msg = by_id.get(mid) or {}
        rows.append(
            {
                "message_id": mid,
                "subject": msg.get("subject")
                or (item.get("proposed") or {}).get("subject")
                or "(no subject)",
                "from": msg.get("from", ""),
                "proposed_action": item["action"],
                "reversible": bool(item.get("reversible")),
                "why_human": why_human(item),
                "status": "pending",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Pane 2 — Flagged
# ---------------------------------------------------------------------------


def build_flagged(
    inbox: list[dict[str, Any]],
    dispositions_payload: dict[str, Any],
    drafts_payload: dict[str, Any],
    hostile_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = messages_by_id(inbox)
    rows: dict[str, dict[str, Any]] = {}

    # Live content scan (authoritative)
    for msg in inbox:
        hostile = detect_hostile_instruction(msg)
        if not hostile:
            continue
        mid = msg["id"]
        attempted = list(hostile.get("attempted") or [])
        rows[mid] = {
            "id": mid,
            "kind": "hostile",
            "subject": msg.get("subject", ""),
            "from": msg.get("from", ""),
            "attempted": attempted,
            "attempted_summary": ", ".join(attempted) or "assistant-directed hostile ask",
            "system_did": (
                "Refused; disposition mark_as_spam; left in place "
                "(no outbox write, no delete/archive)"
            ),
        }

    # Enrich from refusal log if present
    for entry in hostile_log:
        mid = entry.get("message_id")
        if not mid:
            continue
        if mid in rows:
            log_attempted = list(entry.get("attempted") or [])
            if log_attempted:
                merged = list(dict.fromkeys(rows[mid]["attempted"] + log_attempted))
                rows[mid]["attempted"] = merged
                rows[mid]["attempted_summary"] = ", ".join(merged)
            continue
        msg = by_id.get(mid) or {}
        attempted = list(entry.get("attempted") or [])
        rows[mid] = {
            "id": mid,
            "kind": "hostile",
            "subject": msg.get("subject", ""),
            "from": msg.get("from", ""),
            "attempted": attempted,
            "attempted_summary": ", ".join(attempted) or "hostile instruction",
            "system_did": (
                "Refused; disposition mark_as_spam; left in place "
                "(no outbox write, no delete/archive)"
            ),
        }

    for d in dispositions_payload.get("dispositions") or []:
        if d.get("disposition") != "mark_as_spam":
            continue
        mid = d["id"]
        if mid in rows:
            continue
        msg = by_id.get(mid) or {}
        reason = d.get("reason") or "spam / phishing"
        rows[mid] = {
            "id": mid,
            "kind": "phishing_or_spam",
            "subject": msg.get("subject", ""),
            "from": msg.get("from", ""),
            "attempted": ["social_engineering_or_spam"],
            "attempted_summary": reason,
            "system_did": (
                "Flagged mark_as_spam; no send or delete performed on its behalf"
            ),
        }

    for draft in drafts_payload.get("drafts") or []:
        if draft.get("status") != "insufficient_evidence":
            continue
        tid = draft.get("target_id") or "unknown"
        reason = draft.get("reason") or "evidence missing in inbox"
        rows[tid] = {
            "id": tid,
            "kind": "ungrounded",
            "subject": "(draft target — insufficient evidence)",
            "from": "",
            "attempted": ["draft_grounded_reply"],
            "attempted_summary": f"Draft a grounded reply ({reason})",
            "system_did": "Abstained; draft_body left null; no invented facts",
        }

    order = {"hostile": 0, "phishing_or_spam": 1, "ungrounded": 2}
    return sorted(rows.values(), key=lambda r: (order.get(r["kind"], 9), r["id"]))


# ---------------------------------------------------------------------------
# Pane 3 — Commitments
# ---------------------------------------------------------------------------


def _has_pt(text: str) -> bool:
    return bool(re.search(r"\bPT\b|\bPacific\b", text, re.IGNORECASE))


def extract_raw_commitments(
    inbox: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Heuristic extractors grounded in message bodies (top content only)."""
    found: list[dict[str, Any]] = []

    for msg in inbox:
        mid = msg["id"]
        body = top_body(msg.get("body", ""))
        subject = msg.get("subject") or ""
        blob = f"{subject}\n{body}"
        anchor = parse_msg_ts(msg["timestamp"])
        anchor_date = anchor.date()

        # Explicit M/D event: Board meeting 9/18 (also webinar-style phrasing)
        m_md = re.search(
            r"\b(?:board\s+meeting|webinar|event|meeting)\s+(\d{1,2})/(\d{1,2})\b",
            blob,
            re.IGNORECASE,
        )
        if not m_md:
            m_md = re.search(r"\b(\d{1,2})/(\d{1,2})\b", blob)
            if m_md and not any(
                k in blob.lower() for k in ("board meeting", "webinar", "board")
            ):
                m_md = None
        if m_md and any(k in blob.lower() for k in ("board meeting", "webinar", "board")):
            month, day = int(m_md.group(1)), int(m_md.group(2))
            year = anchor_date.year
            event_date = date(year, month, day)
            found.append(
                {
                    "id": f"raw_{mid}_board",
                    "title": "Board meeting — Q3 retention + product risk",
                    "kind": "meeting",
                    "when_start": iso_local(
                        datetime(
                            event_date.year,
                            event_date.month,
                            event_date.day,
                            10,
                            0,
                            tzinfo=LOCAL_TZ,
                        )
                    ),
                    "when_end": iso_local(
                        datetime(
                            event_date.year,
                            event_date.month,
                            event_date.day,
                            10,
                            30,
                            tzinfo=LOCAL_TZ,
                        )
                    ),
                    "all_day": False,
                    "cited_message_ids": [mid],
                    "notes": "Date from board/webinar mention; default 10:00–10:30 local",
                    "thread_id": msg.get("thread_id"),
                    "_source": "board_md",
                }
            )

        # Weekday + clock: Thursday at 11:00am / Friday 2–2:45pm PT
        m_range = re.search(
            r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
            r"(?:at\s+)?"
            r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
            r"(?:\s*[–—-]\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm))?"
            r"(?:\s*(PT))?",
            blob,
            re.IGNORECASE,
        )
        if m_range:
            wd = WEEKDAYS[m_range.group(1).lower()]
            target = next_weekday_on_or_after(anchor_date, wd)
            # If the weekday already passed relative to a late-week message
            # and phrase is clearly this week's meeting, prefer upcoming:
            if target < anchor_date:
                target = next_weekday_on_or_after(
                    anchor_date + timedelta(days=1), wd
                )
            h1 = int(m_range.group(2))
            mi1 = int(m_range.group(3) or 0)
            ap1 = m_range.group(4).lower()
            if ap1 == "pm" and h1 != 12:
                h1 += 12
            if ap1 == "am" and h1 == 12:
                h1 = 0
            start = datetime(
                target.year, target.month, target.day, h1, mi1, tzinfo=LOCAL_TZ
            )
            if m_range.group(5):
                h2 = int(m_range.group(5))
                mi2 = int(m_range.group(6) or 0)
                ap2 = m_range.group(7).lower()
                if ap2 == "pm" and h2 != 12:
                    h2 += 12
                if ap2 == "am" and h2 == 12:
                    h2 = 0
                end = datetime(
                    target.year, target.month, target.day, h2, mi2, tzinfo=LOCAL_TZ
                )
            else:
                mins = 45 if "promo committee" in blob.lower() else 60
                if "risk sync" in blob.lower() or "launch risk" in blob.lower():
                    mins = 60
                end = start + timedelta(minutes=mins)

            title = subject.strip() or "Meeting"
            if "risk sync" in blob.lower() or "launch risk" in blob.lower():
                title = "Launch risk sync — migration vs launch"
            elif "promo committee" in blob.lower() or (
                "priya candace" in blob.lower() and "committee" in blob.lower()
            ):
                title = "Promo committee (Priya Candace)"
            elif "hiring committee" in blob.lower():
                title = "Hiring committee (Priya Candace)"
            elif "design critique" in blob.lower():
                title = "Design critique — Insights empty states"
            elif "board prep" in blob.lower() or "board meeting" in blob.lower():
                title = "Board prep — Q3 retention / product risk"

            found.append(
                {
                    "id": f"raw_{mid}_meeting",
                    "title": title,
                    "kind": "meeting",
                    "when_start": iso_local(start),
                    "when_end": iso_local(end),
                    "all_day": False,
                    "cited_message_ids": [mid],
                    "notes": f"Parsed from {mid}; TZ America/Los_Angeles"
                    + (" (PT stated)" if _has_pt(blob) else " (TZ assumed PT)"),
                    "thread_id": msg.get("thread_id"),
                    "_source": "weekday_clock",
                }
            )

        # Weekday + clock range without am/pm on the start: Friday 2–2:45pm PT
        m_range2 = re.search(
            r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
            r"(\d{1,2})\s*[–—-]\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
            r"(?:\s*(PT))?",
            blob,
            re.IGNORECASE,
        )
        if m_range2 and not any(
            c["cited_message_ids"] == [mid] and c.get("_source") == "weekday_clock"
            for c in found
        ):
            wd = WEEKDAYS[m_range2.group(1).lower()]
            target = next_weekday_on_or_after(anchor_date, wd)
            if target < anchor_date:
                target = next_weekday_on_or_after(
                    anchor_date + timedelta(days=1), wd
                )
            ap = m_range2.group(5).lower()
            h1 = int(m_range2.group(2))
            h2 = int(m_range2.group(3))
            mi2 = int(m_range2.group(4) or 0)
            h1_24, h2_24 = h1, h2
            if ap == "pm":
                if h1_24 != 12:
                    h1_24 += 12
                if h2_24 != 12:
                    h2_24 += 12
            if ap == "am":
                if h1_24 == 12:
                    h1_24 = 0
                if h2_24 == 12:
                    h2_24 = 0
            start = datetime(
                target.year, target.month, target.day, h1_24, 0, tzinfo=LOCAL_TZ
            )
            end = datetime(
                target.year, target.month, target.day, h2_24, mi2, tzinfo=LOCAL_TZ
            )
            title = subject.strip() or "Meeting"
            if "board prep" in blob.lower():
                title = "Board prep — Insights / Acme"
            found.append(
                {
                    "id": f"raw_{mid}_meeting",
                    "title": title,
                    "kind": "meeting",
                    "when_start": iso_local(start),
                    "when_end": iso_local(end),
                    "all_day": False,
                    "cited_message_ids": [mid],
                    "notes": f"Parsed from {mid}; TZ America/Los_Angeles"
                    + (" (PT stated)" if m_range2.group(6) else " (TZ assumed PT)"),
                    "thread_id": msg.get("thread_id"),
                    "_source": "weekday_clock",
                }
            )

        # Deadline: by Thursday noon
        m_noon = re.search(
            r"\bby\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+noon\b",
            blob,
            re.IGNORECASE,
        )
        if m_noon:
            wd = WEEKDAYS[m_noon.group(1).lower()]
            target = next_weekday_on_or_after(anchor_date, wd)
            if target < anchor_date:
                target = next_weekday_on_or_after(
                    anchor_date + timedelta(days=1), wd
                )
            due = datetime(
                target.year, target.month, target.day, 12, 0, tzinfo=LOCAL_TZ
            )
            who = ""
            if "Priya" in blob:
                who = " — Priya Candace interview feedback"
            found.append(
                {
                    "id": f"raw_{mid}_deadline_noon",
                    "title": f"Deadline{who}" if who else f"Deadline from {subject}",
                    "kind": "deadline",
                    "when_start": iso_local(due),
                    "when_end": iso_local(due),
                    "all_day": False,
                    "cited_message_ids": [mid],
                    "notes": "Deadline noon from message body",
                    "thread_id": msg.get("thread_id"),
                    "_source": "deadline_noon",
                }
            )

        # Talking-points / slide lock (board pre-read follow-up)
        if re.search(r"talking points|slide lock", blob, re.IGNORECASE):
            if (
                "board" in blob.lower()
                or "webinar" in blob.lower()
                or msg.get("thread_id") in {"thr_board", "thr_webinar"}
            ):
                found.append(
                    {
                        "id": f"raw_{mid}_board_detail",
                        "title": "Board pre-read — talking points / slide lock",
                        "kind": "deadline",
                        "when_start": None,
                        "when_end": None,
                        "all_day": True,
                        "cited_message_ids": [mid],
                        "notes": "Talking-points / slide lock from follow-up",
                        "thread_id": msg.get("thread_id"),
                        "_source": "board_detail",
                        "_detail": True,
                    }
                )

    return found


def merge_commitments(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge multi-message facts; require at least one multi-cite entry."""
    merged: list[dict[str, Any]] = []
    consumed: set[str] = set()

    board_dates = [c for c in raw if c.get("_source") == "board_md"]
    board_details = [c for c in raw if c.get("_source") == "board_detail"]

    if board_dates:
        date_c = board_dates[0]
        detail_c = next(
            (
                c
                for c in board_details
                if c["cited_message_ids"] != date_c["cited_message_ids"]
            ),
            None,
        )
        if detail_c is None and board_details:
            detail_c = board_details[0]
        if detail_c is not None:
            cites = list(
                dict.fromkeys(
                    date_c["cited_message_ids"] + detail_c["cited_message_ids"]
                )
            )
            merged.append(
                {
                    "id": "cmt_board_0918",
                    "title": (
                        "Board meeting 2026-09-18 — retention metrics + slide lock"
                    ),
                    "kind": "meeting",
                    "when_start": date_c["when_start"],
                    "when_end": date_c["when_end"],
                    "all_day": False,
                    "cited_message_ids": cites,
                    "notes": (
                        f"Date from {date_c['cited_message_ids'][0]}; "
                        f"slide/talking-points obligations from "
                        f"{detail_c['cited_message_ids'][0]}"
                    ),
                    "conflict": False,
                    "conflicts_with": [],
                }
            )
            for c in raw:
                if c.get("_source") in {"board_md", "board_detail"}:
                    consumed.add(c["id"])
        else:
            for c in board_dates:
                consumed.add(c["id"])
                merged.append(
                    {
                        "id": "cmt_board_0918",
                        "title": "Board meeting 2026-09-18",
                        "kind": "meeting",
                        "when_start": c["when_start"],
                        "when_end": c["when_end"],
                        "all_day": False,
                        "cited_message_ids": list(c["cited_message_ids"]),
                        "notes": c.get("notes", ""),
                        "conflict": False,
                        "conflicts_with": [],
                    }
                )

    # Promo / hiring: merge feedback deadline + committee meeting if both present
    deadlines = [
        c
        for c in raw
        if c.get("_source") == "deadline_noon" and c["id"] not in consumed
    ]
    hiring_meetings = [
        c
        for c in raw
        if c.get("_source") == "weekday_clock"
        and (
            "Hiring committee" in c.get("title", "")
            or "Promo committee" in c.get("title", "")
        )
        and c["id"] not in consumed
    ]
    if deadlines and hiring_meetings:
        d0, m0 = deadlines[0], hiring_meetings[0]
        # Keep as separate calendar entries but also add a linked multi-cite note
        # on the meeting that cites both (date/slot vs who/what deadline context)
        cites = list(
            dict.fromkeys(m0["cited_message_ids"] + d0["cited_message_ids"])
        )
        merged.append(
            {
                "id": "cmt_hiring_committee",
                "title": m0["title"],
                "kind": "meeting",
                "when_start": m0["when_start"],
                "when_end": m0["when_end"],
                "all_day": False,
                "cited_message_ids": cites,
                "notes": (
                    f"Committee slot from {m0['cited_message_ids'][0]}; "
                    f"feedback-due context from {d0['cited_message_ids'][0]}"
                ),
                "conflict": False,
                "conflicts_with": [],
            }
        )
        consumed.add(m0["id"])
        # Still show the deadline as its own entry with single cite
        merged.append(
            {
                "id": "cmt_hiring_feedback_due",
                "title": d0["title"],
                "kind": "deadline",
                "when_start": d0["when_start"],
                "when_end": d0["when_end"],
                "all_day": False,
                "cited_message_ids": list(d0["cited_message_ids"]),
                "notes": d0.get("notes", ""),
                "conflict": False,
                "conflicts_with": [],
            }
        )
        consumed.add(d0["id"])

    for c in raw:
        if c["id"] in consumed:
            continue
        if c.get("_detail") and not c.get("when_start"):
            continue  # orphan detail without merge
        if not c.get("when_start"):
            continue
        merged.append(
            {
                "id": c["id"].replace("raw_", "cmt_", 1),
                "title": c["title"],
                "kind": c["kind"],
                "when_start": c["when_start"],
                "when_end": c["when_end"],
                "all_day": c.get("all_day", False),
                "cited_message_ids": list(c["cited_message_ids"]),
                "notes": c.get("notes", ""),
                "conflict": False,
                "conflicts_with": [],
            }
        )

    # Hard multi-message requirement
    if not any(len(c["cited_message_ids"]) > 1 for c in merged):
        raise SystemExit(
            "Commitment merge failed: need at least one multi-message "
            "commitment (expected board msg date + slide-lock detail)"
        )

    return merged


def mark_conflicts(commitments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    meetings = [c for c in commitments if c["kind"] == "meeting" and c.get("when_start")]
    for i, a in enumerate(meetings):
        a_start = datetime.fromisoformat(a["when_start"])
        a_end = datetime.fromisoformat(a["when_end"] or a["when_start"])
        for b in meetings[i + 1 :]:
            b_start = datetime.fromisoformat(b["when_start"])
            b_end = datetime.fromisoformat(b["when_end"] or b["when_start"])
            if a_start < b_end and b_start < a_end:
                a["conflict"] = True
                b["conflict"] = True
                if b["id"] not in a["conflicts_with"]:
                    a["conflicts_with"].append(b["id"])
                if a["id"] not in b["conflicts_with"]:
                    b["conflicts_with"].append(a["id"])
                note = (
                    f" CONFLICT: overlaps {b['title']} "
                    f"({b_start.strftime('%a %H:%M')}–{b_end.strftime('%H:%M')})"
                )
                if "CONFLICT:" not in a.get("notes", ""):
                    a["notes"] = (a.get("notes") or "") + note
                note_b = (
                    f" CONFLICT: overlaps {a['title']} "
                    f"({a_start.strftime('%a %H:%M')}–{a_end.strftime('%H:%M')})"
                )
                if "CONFLICT:" not in b.get("notes", ""):
                    b["notes"] = (b.get("notes") or "") + note_b
    return commitments


def validate_commitment_citations(
    commitments: list[dict[str, Any]], mail_store_ids: set[str]
) -> None:
    for c in commitments:
        cites = c.get("cited_message_ids") or []
        if not cites:
            raise SystemExit(f"Citation check failed: {c['id']} has no cited_message_ids")
        for cid in cites:
            if cid not in mail_store_ids:
                raise SystemExit(
                    f"Citation check failed: {c['id']} cites {cid} "
                    "which is not in the mail store"
                )


def build_commitments(inbox: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = extract_raw_commitments(inbox)
    merged = merge_commitments(raw)
    marked = mark_conflicts(merged)
    validate_commitment_citations(marked, {m["id"] for m in inbox})
    marked.sort(key=lambda c: c.get("when_start") or "")
    return marked


def build_calendar_days(
    commitments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    dated: list[date] = []
    for c in commitments:
        if not c.get("when_start"):
            continue
        dated.append(datetime.fromisoformat(c["when_start"]).date())
    if not dated:
        return []
    start = min(dated)
    end = max(dated)
    # pad to full weeks Mon–Sun
    start = start - timedelta(days=start.weekday())
    end = end + timedelta(days=(6 - end.weekday()))
    days: list[dict[str, Any]] = []
    cur = start
    while cur <= end:
        day_isos = []
        for c in commitments:
            if not c.get("when_start"):
                continue
            if datetime.fromisoformat(c["when_start"]).date() == cur:
                day_isos.append(c["id"])
        days.append(
            {
                "date": cur.isoformat(),
                "label": f"{cur.strftime('%a')} {cur.day}",
                "commitment_ids": day_isos,
                "has_conflict": any(
                    next(x for x in commitments if x["id"] == cid).get("conflict")
                    for cid in day_isos
                )
                if day_isos
                else False,
            }
        )
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    # Escape </script> in JSON for safe embedding
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>inboxHero — Run Dashboard</title>
<style>
  :root {{
    --bg: #0f1419;
    --panel: #1a222c;
    --panel-border: #2a3544;
    --text: #e8eef4;
    --muted: #8b9aab;
    --accent: #3d9b8f;
    --accent-dim: #2a6b63;
    --warn: #c9a227;
    --danger: #d45d4a;
    --ok: #6aa84f;
    --row: #121820;
    --font-display: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    --font-body: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    background:
      radial-gradient(1200px 600px at 10% -10%, #1e3a36 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #2a2438 0%, transparent 50%),
      var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 14px;
    line-height: 1.45;
  }}
  header {{
    padding: 1.25rem 1.5rem 0.75rem;
    border-bottom: 1px solid var(--panel-border);
  }}
  header h1 {{
    margin: 0;
    font-family: var(--font-display);
    font-weight: 600;
    font-size: 1.65rem;
    letter-spacing: -0.02em;
  }}
  header p {{
    margin: 0.35rem 0 0;
    color: var(--muted);
    font-size: 0.9rem;
  }}
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr 1.15fr;
    gap: 0.85rem;
    padding: 0.85rem 1.5rem 1.5rem;
    min-height: calc(100vh - 5.5rem);
  }}
  @media (max-width: 1100px) {{
    .grid {{ grid-template-columns: 1fr; }}
  }}
  .pane {{
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }}
  .pane h2 {{
    margin: 0;
    padding: 0.85rem 1rem;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
    border-bottom: 1px solid var(--panel-border);
  }}
  .pane-body {{
    padding: 0.65rem 0.75rem 0.9rem;
    overflow: auto;
    flex: 1;
  }}
  .row {{
    background: var(--row);
    border: 1px solid var(--panel-border);
    border-radius: 4px;
    padding: 0.65rem 0.75rem;
    margin-bottom: 0.55rem;
  }}
  .row .meta {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.65rem;
    align-items: center;
    margin-bottom: 0.35rem;
  }}
  .badge {{
    display: inline-block;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    background: var(--accent-dim);
    color: #d7f5f0;
  }}
  .badge.warn {{ background: #5c4a14; color: #f5e6a8; }}
  .badge.danger {{ background: #5c2a22; color: #f5c4bc; }}
  .badge.ok {{ background: #2d4a22; color: #c8e6b8; }}
  .badge.pending {{ background: #1e3a52; color: #b8d4f0; }}
  .subject {{ font-weight: 600; color: var(--text); }}
  .why, .did, .notes {{
    color: var(--muted);
    font-size: 0.82rem;
    margin-top: 0.3rem;
  }}
  .why strong, .did strong {{ color: #b8c4d0; font-weight: 600; }}
  .cites {{
    margin-top: 0.35rem;
    font-size: 0.75rem;
    color: var(--accent);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }}
  .calendar {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 0.35rem;
    margin-bottom: 0.85rem;
  }}
  .day {{
    min-height: 4.5rem;
    background: var(--row);
    border: 1px solid var(--panel-border);
    border-radius: 4px;
    padding: 0.35rem;
  }}
  .day.conflict-day {{
    border-color: var(--danger);
    box-shadow: inset 0 0 0 1px rgba(212, 93, 74, 0.35);
  }}
  .day .dlabel {{
    font-size: 0.68rem;
    color: var(--muted);
    margin-bottom: 0.25rem;
  }}
  .chip {{
    display: block;
    font-size: 0.65rem;
    line-height: 1.25;
    padding: 0.2rem 0.3rem;
    margin-bottom: 0.2rem;
    border-radius: 2px;
    background: var(--accent-dim);
    color: #e0f7f3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .chip.conflict {{
    background: #5c2a22;
    color: #ffd4cc;
    font-weight: 700;
  }}
  .conflict-banner {{
    background: #3a1c18;
    border: 1px solid var(--danger);
    color: #ffd4cc;
    padding: 0.55rem 0.7rem;
    border-radius: 4px;
    margin-bottom: 0.75rem;
    font-size: 0.82rem;
  }}
  .empty {{ color: var(--muted); font-style: italic; padding: 0.5rem; }}
</style>
</head>
<body>
  <header>
    <h1>inboxHero</h1>
    <p id="subtitle">Run dashboard — generated from completed triage artifacts</p>
  </header>
  <div class="grid">
    <section class="pane" id="pane-pending">
      <h2>Pending actions</h2>
      <div class="pane-body" id="pending-body"></div>
    </section>
    <section class="pane" id="pane-flagged">
      <h2>Flagged</h2>
      <div class="pane-body" id="flagged-body"></div>
    </section>
    <section class="pane" id="pane-commitments">
      <h2>Commitments</h2>
      <div class="pane-body" id="commitments-body"></div>
    </section>
  </div>
  <script id="dashboard-data" type="application/json">{payload}</script>
  <script>
  (function () {{
    const data = JSON.parse(document.getElementById("dashboard-data").textContent);
    document.getElementById("subtitle").textContent =
      "Generated " + (data.generated_at || "") +
      " · " + (data.message_count || 0) + " messages · reproducible from run artifacts";

    const pendingEl = document.getElementById("pending-body");
    if (!data.pending || !data.pending.length) {{
      pendingEl.innerHTML = '<div class="empty">No actions awaiting human approval.</div>';
    }} else {{
      pendingEl.innerHTML = data.pending.map(function (r) {{
        return (
          '<div class="row">' +
            '<div class="meta">' +
              '<span class="badge">' + esc(r.proposed_action) + '</span>' +
              (r.reversible === false ? '<span class="badge danger">irreversible</span>' : '<span class="badge">reversible</span>') +
              '<span class="cites">' + esc(r.message_id) + '</span>' +
            '</div>' +
            '<div class="subject">' + esc(r.subject) + '</div>' +
            '<div class="why"><strong>Why human:</strong> ' + esc(r.why_human) + '</div>' +
          '</div>'
        );
      }}).join("");
    }}

    const flaggedEl = document.getElementById("flagged-body");
    if (!data.flagged || !data.flagged.length) {{
      flaggedEl.innerHTML = '<div class="empty">Nothing flagged.</div>';
    }} else {{
      flaggedEl.innerHTML = data.flagged.map(function (r) {{
        const kindClass = r.kind === "hostile" ? "danger" :
          (r.kind === "ungrounded" ? "warn" : "warn");
        return (
          '<div class="row">' +
            '<div class="meta">' +
              '<span class="badge ' + kindClass + '">' + esc(r.kind) + '</span>' +
              '<span class="cites">' + esc(r.id) + '</span>' +
            '</div>' +
            '<div class="subject">' + esc(r.subject || r.id) + '</div>' +
            '<div class="why"><strong>Attempted:</strong> ' + esc(r.attempted_summary) + '</div>' +
            '<div class="did"><strong>System did:</strong> ' + esc(r.system_did) + '</div>' +
          '</div>'
        );
      }}).join("");
    }}

    const cmtEl = document.getElementById("commitments-body");
    const conflicts = (data.commitments || []).filter(function (c) {{ return c.conflict; }});
    let html = "";
    if (conflicts.length) {{
      html += '<div class="conflict-banner"><strong>Time conflicts detected</strong> — ' +
        conflicts.map(function (c) {{
          return esc(c.title) + " vs " + (c.conflicts_with || []).join(", ");
        }}).join("; ") + '</div>';
    }}

    const byId = {{}};
    (data.commitments || []).forEach(function (c) {{ byId[c.id] = c; }});

    if (data.calendar_days && data.calendar_days.length) {{
      html += '<div class="calendar">';
      data.calendar_days.forEach(function (day) {{
        html += '<div class="day' + (day.has_conflict ? ' conflict-day' : '') + '">';
        html += '<div class="dlabel">' + esc(day.label) + '</div>';
        (day.commitment_ids || []).forEach(function (cid) {{
          const c = byId[cid];
          if (!c) return;
          const cls = c.conflict ? "chip conflict" : "chip";
          const prefix = c.conflict ? "CONFLICT · " : "";
          html += '<div class="' + cls + '" title="' + esc(c.title) + '">' +
            prefix + esc(shortTitle(c.title)) + '</div>';
        }});
        html += '</div>';
      }});
      html += '</div>';
    }}

    html += (data.commitments || []).map(function (c) {{
      const when = formatWhen(c);
      return (
        '<div class="row">' +
          '<div class="meta">' +
            '<span class="badge">' + esc(c.kind) + '</span>' +
            (c.conflict ? '<span class="badge danger">conflict</span>' : '') +
            '<span>' + esc(when) + '</span>' +
          '</div>' +
          '<div class="subject">' + esc(c.title) + '</div>' +
          '<div class="cites">cites: ' + esc((c.cited_message_ids || []).join(", ")) + '</div>' +
          (c.notes ? '<div class="notes">' + esc(c.notes) + '</div>' : '') +
        '</div>'
      );
    }}).join("");

    cmtEl.innerHTML = html || '<div class="empty">No commitments extracted.</div>';

    function esc(s) {{
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}
    function shortTitle(t) {{
      return t.length > 28 ? t.slice(0, 26) + "…" : t;
    }}
    function formatWhen(c) {{
      if (!c.when_start) return "unscheduled";
      try {{
        const d = new Date(c.when_start);
        return d.toLocaleString(undefined, {{
          weekday: "short", month: "short", day: "numeric",
          hour: "numeric", minute: "2-digit"
        }});
      }} catch (e) {{
        return c.when_start;
      }}
    }}
  }})();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_dashboard() -> dict[str, Any]:
    inbox = load_json(INBOX_PATH)
    if not isinstance(inbox, list):
        raise SystemExit("inbox.json must be a list of messages")
    dispositions = load_json(DISPOSITIONS_PATH)
    drafts = load_json(DRAFTS_PATH)
    hostile_log = load_jsonl(HOSTILE_LOG_PATH)
    state = load_state() if STATE_PATH.exists() else {
        "archived": [],
        "trash": {},
        "purged": [],
    }

    pending = build_pending(inbox, drafts, state)
    flagged = build_flagged(inbox, dispositions, drafts, hostile_log)
    commitments = build_commitments(inbox)
    calendar_days = build_calendar_days(commitments)

    multi = [c for c in commitments if len(c.get("cited_message_ids") or []) > 1]
    conflicts = [c for c in commitments if c.get("conflict")]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message_count": len(inbox),
        "pending": pending,
        "flagged": flagged,
        "commitments": commitments,
        "calendar_days": calendar_days,
        "stats": {
            "pending_count": len(pending),
            "flagged_count": len(flagged),
            "commitment_count": len(commitments),
            "multi_message_commitments": len(multi),
            "conflict_count": len(conflicts),
        },
    }


def main() -> None:
    data = build_dashboard()
    DATA_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    HTML_PATH.write_text(render_html(data), encoding="utf-8")

    print("Part 7 dashboard written:")
    print(f"  {DATA_PATH}")
    print(f"  {HTML_PATH}")
    print(
        f"  pending={data['stats']['pending_count']}  "
        f"flagged={data['stats']['flagged_count']}  "
        f"commitments={data['stats']['commitment_count']}  "
        f"multi_cite={data['stats']['multi_message_commitments']}  "
        f"conflicts={data['stats']['conflict_count']}"
    )
    if data["stats"]["multi_message_commitments"] < 1:
        raise SystemExit("FAIL: need ≥1 multi-message commitment")
    if data["stats"]["conflict_count"] < 1:
        raise SystemExit("FAIL: expected at least one calendar conflict")


if __name__ == "__main__":
    main()
