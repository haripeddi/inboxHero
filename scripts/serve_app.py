#!/usr/bin/env python3
"""Interactive Gmail-like demo UI (X4). Assignment R6 dashboard stays separate."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import config as app_config  # noqa: E402
from calendar_store import CALENDAR_PATH, load_calendar  # noqa: E402
from llm import complete as llm_complete_pair  # noqa: E402
from memory import remember, working_context  # noqa: E402
from run_actions import (  # noqa: E402
    OWNER,
    OUTBOX_DIR,
    apply_archive,
    apply_soft_delete,
    load_state,
    log_decision,
    save_state,
    write_outbox_queued,
)

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
    import uvicorn
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "FastAPI/uvicorn required. Install with: pip install fastapi uvicorn"
    ) from exc

INBOX_PATH = ROOT / "inbox.json"
DISPOSITIONS_PATH = ROOT / "dispositions.json"
DRAFTS_PATH = ROOT / "drafts.json"
DASHBOARD_DATA_PATH = ROOT / "dashboard_data.json"
CAPABILITIES_PATH = ROOT / "capabilities.json"
SLACK_PATH = ROOT / "slack.json"
APP_DIR = ROOT / "app"
PROMPTS_PATH = ROOT / "config" / "agent_prompts.json"
MEMORY_PATH = ROOT / "memory" / "standing_instructions.json"

BASE_SYSTEM = (
    "You are inboxHero assistant for Alex Rivera (VP Product). "
    "Answer ONLY from the provided email/slack/prefs context. "
    "Cite message ids like msg_064. Never invent facts. "
    "If evidence is missing, say so. Never auto-approve spend over $5k."
)

app = FastAPI(title="inboxHero Demo UI", version="1.0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _inbox() -> list[dict[str, Any]]:
    data = _load_json(INBOX_PATH, [])
    if not isinstance(data, list):
        raise HTTPException(500, "inbox.json must be an array")
    return data


def _by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in messages}


def _disp_map() -> dict[str, dict[str, Any]]:
    raw = _load_json(DISPOSITIONS_PATH, {}) or {}
    rows = raw.get("dispositions") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return {}
    return {d["id"]: d for d in rows if isinstance(d, dict) and "id" in d}


def _draft_map() -> dict[str, dict[str, Any]]:
    raw = _load_json(DRAFTS_PATH, {}) or {}
    rows = raw.get("drafts") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for d in rows:
        if not isinstance(d, dict):
            continue
        mid = d.get("target_id") or d.get("message_id") or d.get("id")
        if mid:
            out[str(mid)] = d
    return out


def _drafts_list(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = _by_id(messages)
    rows: list[dict[str, Any]] = []
    for mid, d in _draft_map().items():
        msg = by_id.get(mid) or {}
        body = d.get("draft_body") or d.get("body") or ""
        rows.append(
            {
                "target_id": mid,
                "status": d.get("status") or "drafted",
                "subject": msg.get("subject") or f"Draft for {mid}",
                "from": msg.get("from") or OWNER,
                "to": msg.get("to"),
                "snippet": body[:160].replace("\n", " "),
                "draft_body": body,
                "cited_message_ids": d.get("cited_message_ids") or [],
                "notes": d.get("notes"),
                "retrieval": d.get("retrieval") or {},
            }
        )
    rows.sort(key=lambda r: r.get("target_id") or "")
    return rows


def _outbox_list() -> list[dict[str, Any]]:
    if not OUTBOX_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(OUTBOX_DIR.glob("*.json")):
        data = _load_json(path, {}) or {}
        if not isinstance(data, dict):
            continue
        body = (data.get("body") or "")[:160].replace("\n", " ")
        rows.append(
            {
                "outbox_id": data.get("outbox_id") or path.stem,
                "path": str(path.relative_to(ROOT)),
                "in_reply_to": data.get("in_reply_to") or data.get("message_id"),
                "thread_id": data.get("thread_id"),
                "from": data.get("from"),
                "to": data.get("to"),
                "subject": data.get("subject"),
                "snippet": body,
                "status": data.get("status") or "queued",
                "kind": data.get("kind"),
                "approved_at": data.get("approved_at"),
                "body": data.get("body") or "",
                "cited_message_ids": data.get("cited_message_ids") or [],
            }
        )
    rows.sort(key=lambda r: r.get("approved_at") or r.get("outbox_id") or "", reverse=True)
    return rows


def _spam_list(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in _list_summary(messages) if m.get("disposition") == "mark_as_spam"]


def _clear_read_state() -> None:
    state = load_state()
    state["read"] = []
    save_state(state)


def _action_log(
    steps: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    dash: dict[str, Any],
) -> list[str]:
    """Narrative lines for the landing ticker — grounded in real artifacts."""
    lines: list[str] = [
        "Starting inboxHero assistant…",
        "Loading mailbox corpus…",
    ]
    for step in steps:
        name = step.get("script") or "pipeline"
        ok = step.get("ok")
        if ok:
            lines.append(f"Ran {name} ✓")
        elif "error" in step:
            lines.append(f"Skipped {name} ({step.get('error')})")
        else:
            lines.append(f"Finished {name}")

    disp = _disp_map()
    spam_ids = [mid for mid, d in disp.items() if d.get("disposition") == "mark_as_spam"]
    for mid in spam_ids[:4]:
        subj = next((m.get("subject") for m in messages if m.get("id") == mid), mid)
        lines.append(f"Flagging spam · {mid} — {subj}")

    for d in _drafts_list(messages)[:4]:
        lines.append(f"Staging draft · {d['target_id']} — {d.get('subject')}")

    for row in (dash.get("pending") or [])[:3]:
        mid = row.get("message_id") or "?"
        action = row.get("proposed_action") or "gated action"
        lines.append(f"Holding for human · {mid} — {action}")

    for c in (dash.get("commitments") or [])[:3]:
        title = c.get("title") or c.get("id")
        lines.append(f"Scheduling commitment · {title}")

    for o in _outbox_list()[:2]:
        lines.append(f"Queued outbox · {o.get('outbox_id')} — {o.get('subject')}")

    lines.append("Marking inbox unread for a fresh session…")
    lines.append("Agent Mode ready")
    return lines


def _preferences_payload() -> dict[str, Any]:
    raw = _load_json(MEMORY_PATH, {"facts": {}}) or {"facts": {}}
    facts_map = raw.get("facts") if isinstance(raw, dict) else {}
    if not isinstance(facts_map, dict):
        facts_map = {}
    facts = []
    for key, fact in sorted(facts_map.items()):
        if not isinstance(fact, dict):
            continue
        facts.append(
            {
                "key": fact.get("key") or key,
                "value": fact.get("value") or "",
                "source": fact.get("source") or "",
                "updated_at": fact.get("updated_at") or "",
            }
        )
    return {"facts": facts, "count": len(facts), "path": "memory/standing_instructions.json"}


def _default_prompts() -> list[dict[str, Any]]:
    data = _load_json(PROMPTS_PATH, {"prompts": []}) or {"prompts": []}
    rows = data.get("prompts") if isinstance(data, dict) else []
    return list(rows) if isinstance(rows, list) else []


def _ensure_prompts_file() -> list[dict[str, Any]]:
    PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PROMPTS_PATH.exists():
        _save_json(PROMPTS_PATH, {"prompts": []})
    return _default_prompts()


def _save_prompts(prompts: list[dict[str, Any]]) -> None:
    PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _save_json(PROMPTS_PATH, {"prompts": prompts})


def _prompt_by_mode(mode: str) -> dict[str, Any] | None:
    for p in _ensure_prompts_file():
        if not isinstance(p, dict):
            continue
        if p.get("mode") == mode and p.get("enabled", True):
            return p
    return None


def _prompt_by_id(pid: str) -> dict[str, Any] | None:
    for p in _ensure_prompts_file():
        if isinstance(p, dict) and p.get("id") == pid:
            return p
    return None


def _fill_template(template: str, values: dict[str, str]) -> str:
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", val)
    return out


def _extract_addrs(line: str | None) -> list[str]:
    if not line:
        return []
    parts = re.split(r"[,;]", line)
    return [p.strip() for p in parts if p.strip()]


def _owner_addrs() -> set[str]:
    return {OWNER.lower(), "alex.rivera@northstarlabs.com", "alex rivera"}


def _is_owner(addr: str) -> bool:
    low = addr.lower()
    for o in _owner_addrs():
        if o in low:
            return True
    return False


def _thread_text(thr: list[dict[str, Any]]) -> str:
    parts = []
    for m in thr[-8:]:
        parts.append(
            f"[{m['id']}] from={m.get('from')} subject={m.get('subject')}\n"
            f"{(m.get('body') or '')[:1000]}"
        )
    return "\n\n".join(parts)


def _high_stakes(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in (
            "approve",
            "approval",
            "$",
            "budget",
            "spend",
            "protoforge",
            "20,000",
            "20000",
            "purchase order",
            "legal",
        )
    )


def _fallback_compose(msg: dict[str, Any], mode: str, cites: list[str]) -> str:
    mid = msg["id"]
    subj = msg.get("subject") or ""
    body = (msg.get("body") or "")[:500]
    if _high_stakes(f"{subj} {body}"):
        return (
            f"Thanks for the note on {subj}.\n\n"
            f"Per standing prefs I don't auto-approve spend over $5k — "
            f"I'm escalating to Priya Shah (CFO) and will confirm once she signs off.\n\n"
            f"— Alex\n\nCited: {', '.join(cites) or mid}"
        )
    if mode == "reply_all":
        return (
            f"Thanks all — looping back on {subj}.\n\n"
            f"I've reviewed the thread ({', '.join(cites) or mid}) and will follow up with next steps shortly.\n\n"
            f"— Alex"
        )
    return (
        f"Thanks for your note on {subj}.\n\n"
        f"Grounded on {', '.join(cites) or mid}: I'll take this from here and reply with a concrete next step.\n\n"
        f"— Alex\n\nSnippet considered:\n{body[:300]}"
    )


def _list_summary(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state = load_state()
    trash = set((state.get("trash") or {}).keys())
    purged = set(state.get("purged") or [])
    read = set(state.get("read") or [])
    archived = set(state.get("archived") or [])
    disp = _disp_map()
    drafts = _draft_map()

    rows: list[dict[str, Any]] = []
    for m in messages:
        mid = m["id"]
        if mid in trash or mid in purged:
            continue
        body = (m.get("body") or "")[:160].replace("\n", " ")
        d = disp.get(mid) or {}
        draft = drafts.get(mid) or {}
        has_draft = bool(draft) and draft.get("status") == "drafted"
        rows.append(
            {
                "id": mid,
                "thread_id": m.get("thread_id"),
                "from": m.get("from"),
                "to": m.get("to"),
                "subject": m.get("subject"),
                "timestamp": m.get("timestamp"),
                "snippet": body,
                "disposition": d.get("disposition"),
                "disposition_reason": d.get("reason"),
                "route": d.get("route"),
                "read": mid in read,
                "archived": mid in archived,
                "has_draft": has_draft,
                "cited_message_ids": draft.get("cited_message_ids") or [],
            }
        )
    rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return rows


def _thread_for(msg: dict[str, Any], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tid = msg.get("thread_id")
    thr = [m for m in messages if m.get("thread_id") == tid]
    thr.sort(key=lambda m: (m.get("timestamp") or "", m.get("id") or ""))
    return thr


def _next_cal_id(events: list[dict[str, Any]]) -> str:
    nums = []
    for e in events:
        m = re.search(r"cal_(\d+)", str(e.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 100
    return f"cal_{n:03d}"


def _llm_reply(system: str, user: str) -> tuple[str, bool]:
    """Return (text, used_llm) via config.py provider (ollama|openai|gemini)."""
    text, used = llm_complete_pair(system, user)
    if used:
        return text, True
    return (
        f"(LLM unavailable — {app_config.llm_summary()} — deterministic fallback)\n\n"
        f"{text}\n\n"
        f"Ask about a specific message id (e.g. msg_064) after selecting it.",
        False,
    )


def _append_trace(event: dict[str, Any]) -> None:
    """Append one JSON line to root trace.jsonl (submission artifact)."""
    path = ROOT / "trace.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _outbox_replied_ids() -> set[str]:
    ids: set[str] = set()
    for row in _outbox_list():
        mid = row.get("in_reply_to") or row.get("message_id")
        if mid:
            ids.add(str(mid))
    return ids


def _live_pending(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Disposition-derived pending queue for X4 (not only Part 4 demo targets).
    Skips trash/purged/archived and messages already answered in outbox.
    """
    state = load_state()
    trash = set((state.get("trash") or {}).keys())
    purged = set(state.get("purged") or [])
    archived = set(state.get("archived") or [])
    replied = _outbox_replied_ids()
    by_id = _by_id(messages)
    drafts = _draft_map()
    rows: list[dict[str, Any]] = []

    for mid, d in _disp_map().items():
        if mid in trash or mid in purged or mid in archived:
            continue
        msg = by_id.get(mid)
        if not msg:
            continue
        disp = (d.get("disposition") or "").strip()
        reason = (d.get("reason") or "").strip()
        draft = drafts.get(mid) or {}
        cites = list(draft.get("cited_message_ids") or [mid])
        action = None
        reversible = True
        if disp in {"reply", "reply_all"} and mid not in replied:
            action = "send"
            reversible = False
        elif disp == "archive":
            action = "archive"
        elif disp == "mark_as_spam" or (
            disp == "move_to_folder"
            and any(x in reason.lower() for x in ("vendor", "receipt", "newsletter", "alert", "spam"))
        ):
            action = "soft_delete"
        elif disp == "escalate":
            action = "escalate"
            reversible = True
        else:
            continue

        why = reason or f"Disposition {disp} requires human gate before {action}."
        rows.append(
            {
                "message_id": mid,
                "from": msg.get("from"),
                "subject": msg.get("subject"),
                "proposed_action": action,
                "disposition": disp,
                "why_human": why,
                "cited_message_ids": cites[:8],
                "status": "pending",
                "reversible": reversible,
                "has_draft": bool(draft.get("status") == "drafted" and (draft.get("draft_body") or draft.get("body"))),
            }
        )

    order = {"escalate": 0, "send": 1, "soft_delete": 2, "archive": 3}
    rows.sort(key=lambda r: (order.get(r["proposed_action"], 9), r["message_id"]))
    return rows[:60]


def _learn_from_compose_edit(
    message_id: str,
    original: str,
    edited: str,
) -> list[dict[str, str]]:
    """Heuristic learning when the user edits a draft before Send."""
    learned: list[dict[str, str]] = []
    orig = (original or "").strip()
    edit = (edited or "").strip()
    if not edit or edit == orig:
        return learned
    low = edit.lower()
    if any(k in low for k in ("priya", "escalate", "$5k", "5000", "cfo")):
        res = remember(
            "never_auto_approve_spend_over",
            "5000; escalate to Priya Shah <priya.shah@northstarlabs.com>",
            source=message_id,
        )
        if res.get("status") == "ok":
            learned.append({"key": res["key"], "value": res["value"], "source": message_id})
    if "async" in low or "prefer email" in low or "no meeting" in low:
        res = remember(
            "prefer_async_updates",
            "Prefer async updates over meetings when possible",
            source=message_id,
        )
        if res.get("status") == "ok":
            learned.append({"key": res["key"], "value": res["value"], "source": message_id})
    # Tone / brevity signal
    if orig and len(edit) < max(40, int(len(orig) * 0.7)):
        res = remember(
            "reply_style_preference",
            "Prefer shorter, direct replies",
            source=message_id,
        )
        if res.get("status") == "ok":
            learned.append({"key": res["key"], "value": res["value"], "source": message_id})
    if not learned and abs(len(edit) - len(orig)) > 40:
        res = remember(
            "last_edited_compose_style",
            f"User rewrote compose for {message_id} (custom tone)",
            source=message_id,
        )
        if res.get("status") == "ok":
            learned.append({"key": res["key"], "value": res["value"], "source": message_id})
    return learned


def _fallback_chat(msg: dict[str, Any] | None, question: str) -> str:
    if not msg:
        return (
            "No message selected. Open an email in Primary, then ask about it. "
            "I will cite message ids from the inbox only."
        )
    mid = msg["id"]
    subj = msg.get("subject", "")
    body = (msg.get("body") or "")[:800]
    q = question.lower()
    cites = [mid]
    if "approve" in q or "spend" in q or "budget" in q or "protoforge" in q.lower():
        return (
            f"From {mid} ({subj}): the thread asks about approval / spend. "
            f"Standing prefs escalate spend ≥ $5k to Priya Shah (CFO) — do not auto-approve. "
            f"Snippet:\n{body[:400]}\n\nCited: {', '.join(cites)}"
        )
    return (
        f"Grounded on {mid}: {subj}\n\n"
        f"{body[:500]}\n\n"
        f"Cited: {mid}. Ask a more specific question if you need another detail."
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    message_id: str | None = None


class InviteRequest(BaseModel):
    title: str = Field(..., min_length=1)
    start: str = Field(..., min_length=1)
    end: str = Field(..., min_length=1)
    attendees: list[str] = Field(default_factory=list)
    notes: str = ""
    related_message_id: str | None = None
    execute: bool = False


class SimulateRequest(BaseModel):
    rebuild: bool = False


class ComposeRequest(BaseModel):
    message_id: str = Field(..., min_length=1)
    mode: str = Field(default="reply")
    extra_cc: str = ""
    extra_bcc: str = ""
    prompt_id: str | None = None
    execute_stage: bool = False


class PromptUpsert(BaseModel):
    id: str | None = None
    label: str = Field(..., min_length=1)
    mode: str = Field(..., min_length=1)
    description: str = ""
    user_template: str = Field(..., min_length=1)
    enabled: bool = True
    trigger_keywords: list[str] = Field(default_factory=list)


class StageComposeRequest(BaseModel):
    message_id: str
    to: str
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = Field(..., min_length=1)
    cited_message_ids: list[str] = Field(default_factory=list)
    execute: bool = False
    mode: str = "reply"
    original_body: str = ""


class PendingActionRequest(BaseModel):
    message_id: str = Field(..., min_length=1)
    action: str = Field(..., min_length=1)
    execute: bool = True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/app/")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    messages = _inbox()
    dash = _load_json(DASHBOARD_DATA_PATH, {}) or {}
    cal = _load_json(CALENDAR_PATH, {"events": []}) or {"events": []}
    caps = _load_json(CAPABILITIES_PATH, {}) or {}
    state = load_state()
    spam = _spam_list(messages)
    drafts = _drafts_list(messages)
    outbox = _outbox_list()
    prefs = _preferences_payload()
    pending = _live_pending(messages)
    primary = [m for m in _list_summary(messages) if not m.get("archived")]

    return {
        "owner": OWNER,
        "messages": primary,
        "message_count": len(primary),
        "pending": pending,
        "flagged": dash.get("flagged") or [],
        "spam": spam,
        "drafts": drafts,
        "outbox": outbox,
        "commitments": dash.get("commitments") or [],
        "calendar_days": dash.get("calendar_days") or [],
        "stats": {
            **(dash.get("stats") or {}),
            "pending_count": len(pending),
            "spam_count": len(spam),
            "draft_count": len(drafts),
            "outbox_count": len(outbox),
        },
        "calendar_events": cal.get("events") or [],
        "calendar_timezone": cal.get("timezone") or "America/Los_Angeles",
        "preferences": prefs,
        "llm": app_config.llm_summary(),
        "mailbox_state": {
            "read_count": len(state.get("read") or []),
            "trash_count": len(state.get("trash") or {}),
            "archived_count": len(state.get("archived") or []),
            "purged_count": len(state.get("purged") or []),
        },
        "artifacts": {
            "inbox": INBOX_PATH.exists(),
            "dispositions": DISPOSITIONS_PATH.exists(),
            "drafts": DRAFTS_PATH.exists(),
            "dashboard_data": DASHBOARD_DATA_PATH.exists(),
            "calendar": CALENDAR_PATH.exists(),
            "outbox": OUTBOX_DIR.exists(),
            "agent_prompts": PROMPTS_PATH.exists(),
        },
        "capabilities": caps.get("capabilities") or [],
        "system": caps.get("system") or {},
    }


@app.get("/api/messages/{message_id}")
def get_message(message_id: str) -> dict[str, Any]:
    messages = _inbox()
    by_id = _by_id(messages)
    msg = by_id.get(message_id)
    if not msg:
        raise HTTPException(404, f"Unknown message {message_id}")
    thr = _thread_for(msg, messages)
    draft = _draft_map().get(message_id)
    disp = _disp_map().get(message_id)
    return {
        "message": msg,
        "thread": thr,
        "disposition": disp,
        "draft": draft,
    }


@app.post("/api/messages/{message_id}/read")
def mark_read(message_id: str) -> dict[str, Any]:
    messages = _inbox()
    if message_id not in _by_id(messages):
        raise HTTPException(404, f"Unknown message {message_id}")
    state = load_state()
    read = list(state.get("read") or [])
    if message_id not in read:
        read.append(message_id)
        state["read"] = read
        save_state(state)
    return {"id": message_id, "read": True, "read_count": len(read)}


@app.get("/api/outbox/{outbox_id}")
def get_outbox(outbox_id: str) -> dict[str, Any]:
    for row in _outbox_list():
        if row.get("outbox_id") == outbox_id:
            return row
    raise HTTPException(404, f"Unknown outbox item {outbox_id}")


@app.get("/api/agent-prompts")
def list_prompts() -> dict[str, Any]:
    return {"prompts": _ensure_prompts_file(), "path": "config/agent_prompts.json"}


@app.post("/api/agent-prompts")
def create_prompt(req: PromptUpsert) -> dict[str, Any]:
    prompts = _ensure_prompts_file()
    pid = req.id or f"custom_{re.sub(r'[^a-z0-9]+', '_', req.label.lower()).strip('_')}"
    if any(p.get("id") == pid for p in prompts if isinstance(p, dict)):
        raise HTTPException(400, f"Prompt id {pid} already exists")
    row = {
        "id": pid,
        "label": req.label,
        "mode": req.mode,
        "description": req.description,
        "user_template": req.user_template,
        "enabled": req.enabled,
        "trigger_keywords": req.trigger_keywords,
        "seeded": False,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    prompts.append(row)
    _save_prompts(prompts)
    return {"status": "created", "prompt": row}


@app.put("/api/agent-prompts/{prompt_id}")
def update_prompt(prompt_id: str, req: PromptUpsert) -> dict[str, Any]:
    prompts = _ensure_prompts_file()
    found = None
    for i, p in enumerate(prompts):
        if isinstance(p, dict) and p.get("id") == prompt_id:
            found = i
            break
    if found is None:
        raise HTTPException(404, f"Unknown prompt {prompt_id}")
    old = prompts[found]
    row = {
        **old,
        "label": req.label,
        "mode": req.mode,
        "description": req.description,
        "user_template": req.user_template,
        "enabled": req.enabled,
        "trigger_keywords": req.trigger_keywords,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    prompts[found] = row
    _save_prompts(prompts)
    return {"status": "updated", "prompt": row}


@app.delete("/api/agent-prompts/{prompt_id}")
def delete_prompt(prompt_id: str) -> dict[str, Any]:
    prompts = _ensure_prompts_file()
    target = next((p for p in prompts if isinstance(p, dict) and p.get("id") == prompt_id), None)
    if not target:
        raise HTTPException(404, f"Unknown prompt {prompt_id}")
    if target.get("seeded") and not str(prompt_id).startswith("custom_"):
        raise HTTPException(400, "Seeded prompts cannot be deleted — disable or edit instead")
    prompts = [p for p in prompts if not (isinstance(p, dict) and p.get("id") == prompt_id)]
    _save_prompts(prompts)
    return {"status": "deleted", "id": prompt_id}


@app.post("/api/compose")
def compose(req: ComposeRequest) -> dict[str, Any]:
    messages = _inbox()
    by_id = _by_id(messages)
    msg = by_id.get(req.message_id)
    if not msg:
        raise HTTPException(404, f"Unknown message {req.message_id}")

    mode = (req.mode or "reply").strip().lower()
    if mode not in {"reply", "reply_all", "cc", "bcc"}:
        raise HTTPException(400, "mode must be reply|reply_all|cc|bcc")

    thr = _thread_for(msg, messages)
    cites = [m["id"] for m in thr]
    draft_existing = _draft_map().get(req.message_id)
    prefs_text = working_context()
    thread_blob = _thread_text(thr)

    from_addrs = _extract_addrs(msg.get("from"))
    to_addrs = _extract_addrs(msg.get("to"))
    participants = []
    for a in from_addrs + to_addrs:
        if not _is_owner(a) and a not in participants:
            participants.append(a)

    if mode == "reply":
        to_line = ", ".join(from_addrs) or (msg.get("from") or "")
        cc_line = req.extra_cc.strip()
        bcc_line = req.extra_bcc.strip()
    elif mode == "reply_all":
        to_line = ", ".join(participants)
        cc_line = req.extra_cc.strip()
        bcc_line = req.extra_bcc.strip()
    elif mode == "cc":
        to_line = ", ".join(from_addrs) or (msg.get("from") or "")
        cc_line = req.extra_cc.strip() or ", ".join(a for a in participants if a not in from_addrs)
        bcc_line = req.extra_bcc.strip()
    else:  # bcc
        to_line = ", ".join(from_addrs) or (msg.get("from") or "")
        cc_line = req.extra_cc.strip()
        bcc_line = req.extra_bcc.strip() or "priya.shah@northstarlabs.com"

    prompt = _prompt_by_id(req.prompt_id) if req.prompt_id else None
    if not prompt:
        prompt = _prompt_by_mode(mode)
    reason = None
    sources: list[str] = []

    # Prefer grounded drafts.json for plain reply
    body = None
    used_llm = False
    prompt_id = prompt.get("id") if prompt else None
    if mode == "reply" and draft_existing and draft_existing.get("status") == "drafted" and not req.prompt_id:
        body = draft_existing.get("draft_body") or draft_existing.get("body") or ""
        cites = list(draft_existing.get("cited_message_ids") or cites)
        ret = draft_existing.get("retrieval") or {}
        if ret.get("thread_ids_read"):
            sources.append(f"thread_ids_read: {', '.join(ret['thread_ids_read'])}")
        if ret.get("vector_hit_ids"):
            sources.append(f"vector_hit_ids: {', '.join(ret['vector_hit_ids'][:8])}")
        sources.append("drafts.json")
        reason = "existing_draft"

    stake_text = f"{msg.get('subject')} {msg.get('body')} {thread_blob}"
    if body is None and _high_stakes(stake_text):
        high = _prompt_by_mode("high_stakes")
        if high:
            prompt = high
            prompt_id = high.get("id")
            reason = "high_stakes"

    if body is None:
        values = {
            "subject": msg.get("subject") or "",
            "from": msg.get("from") or "",
            "to": to_line,
            "cc": cc_line,
            "bcc": bcc_line,
            "thread": thread_blob,
            "prefs": prefs_text,
        }
        user = _fill_template((prompt or {}).get("user_template") or "", values)
        if not user.strip():
            user = (
                f"Write a grounded {mode} email for Alex.\n"
                f"Subject: {values['subject']}\nThread:\n{thread_blob}\nPrefs:\n{prefs_text}"
            )
            prompt_id = prompt_id or "builtin_compose"
        body, used_llm = _llm_reply(BASE_SYSTEM, user)
        if not used_llm or body.startswith("(Ollama unavailable"):
            body = _fallback_compose(msg, mode, cites)
            used_llm = False
            reason = reason or "fallback"
        sources.append("memory/standing_instructions.json")
        sources.append(f"prompt:{prompt_id or 'builtin'}")
        if not reason:
            reason = "generated"

    subject = msg.get("subject") or ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    return {
        "mode": mode,
        "message_id": req.message_id,
        "to": to_line,
        "cc": cc_line,
        "bcc": bcc_line,
        "subject": subject,
        "body": body,
        "cited_message_ids": cites[:12],
        "sources": sources,
        "used_llm": used_llm,
        "prompt_id": prompt_id,
        "reason": reason,
        "has_existing_draft": bool(draft_existing and draft_existing.get("status") == "drafted"),
    }


@app.post("/api/compose/stage")
def stage_compose(req: StageComposeRequest) -> dict[str, Any]:
    """Legacy dry-run/stage path; prefer /api/compose/send for UI Send."""
    return _compose_send_impl(req, force_execute=req.execute)


@app.post("/api/compose/send")
def compose_send(req: StageComposeRequest) -> dict[str, Any]:
    """Send composed mail to outbox/ and learn from edits."""
    req.execute = True
    return _compose_send_impl(req, force_execute=True)


def _compose_send_impl(req: StageComposeRequest, force_execute: bool) -> dict[str, Any]:
    messages = _inbox()
    msg = _by_id(messages).get(req.message_id)
    if not msg:
        raise HTTPException(404, f"Unknown message {req.message_id}")
    subject = req.subject or msg.get("subject") or "Re: (no subject)"
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    outbox_id = f"out_compose_{req.message_id}_{req.mode}"
    proposed = {
        "outbox_id": outbox_id,
        "in_reply_to": req.message_id,
        "thread_id": msg.get("thread_id"),
        "from": OWNER,
        "to": req.to,
        "cc": req.cc,
        "bcc": req.bcc,
        "subject": subject,
        "body": req.body,
        "cited_message_ids": req.cited_message_ids or [req.message_id],
        "message_id": req.message_id,
        "kind": f"compose_{req.mode}",
    }
    if not force_execute:
        log_decision(
            action="send",
            reversible=False,
            proposed=proposed,
            mode="dry_run",
            human="n/a_dry_run",
            result="previewed",
            detail=f"compose {req.mode} dry-run",
        )
        return {"status": "preview", "execute": False, "proposed": proposed, "learned": []}

    learned = _learn_from_compose_edit(
        req.message_id, req.original_body or "", req.body
    )
    path = write_outbox_queued(proposed)
    log_decision(
        action="send",
        reversible=False,
        proposed=proposed,
        mode="execute",
        human="approved_demo_ui",
        result="queued",
        detail=f"compose sent {path.name}",
    )
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "compose_actions.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "message_id": req.message_id,
                    "mode": req.mode,
                    "edited": bool(learned) or (
                        (req.original_body or "").strip() != (req.body or "").strip()
                        and bool(req.original_body)
                    ),
                    "learned": learned,
                    "outbox": str(path.relative_to(ROOT)),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    _append_trace(
        {
            "event": "compose_send",
            "message_id": req.message_id,
            "mode": req.mode,
            "outbox": str(path.relative_to(ROOT)),
            "learned_keys": [x["key"] for x in learned],
        }
    )
    return {
        "status": "queued",
        "execute": True,
        "outbox_path": str(path.relative_to(ROOT)),
        "proposed": proposed,
        "learned": learned,
    }


@app.post("/api/pending/act")
def pending_act(req: PendingActionRequest) -> dict[str, Any]:
    """Approve a pending gated action from the X4 UI."""
    messages = _inbox()
    by_id = _by_id(messages)
    msg = by_id.get(req.message_id)
    if not msg:
        raise HTTPException(404, f"Unknown message {req.message_id}")
    action = req.action.strip().lower()
    pending_rows = {r["message_id"]: r for r in _live_pending(messages)}
    row = pending_rows.get(req.message_id)
    if not row and action != "send":
        raise HTTPException(400, f"{req.message_id} is not in the pending queue")

    state = load_state()
    result = ""
    if action == "send":
        draft = _draft_map().get(req.message_id) or {}
        body = draft.get("draft_body") or draft.get("body") or ""
        if not body:
            raise HTTPException(400, "No draft body to send — open Reply and Send from compose")
        from_addrs = _extract_addrs(msg.get("from"))
        proposed = {
            "outbox_id": f"out_{req.message_id}",
            "in_reply_to": req.message_id,
            "thread_id": msg.get("thread_id"),
            "from": OWNER,
            "to": ", ".join(from_addrs) or (msg.get("from") or ""),
            "subject": f"Re: {msg.get('subject') or ''}".replace("Re: Re:", "Re:"),
            "body": body,
            "cited_message_ids": list(draft.get("cited_message_ids") or [req.message_id]),
        }
        if not req.execute:
            return {"status": "preview", "proposed": proposed}
        path = write_outbox_queued(proposed)
        result = f"queued {path.name}"
        log_decision(
            action="send",
            reversible=False,
            proposed=proposed,
            mode="execute",
            human="approved_pending_ui",
            result="queued",
            detail=result,
        )
        _append_trace({"event": "pending_send", "message_id": req.message_id, "detail": result})
    elif action == "soft_delete":
        snapshot = {
            "id": msg["id"],
            "thread_id": msg.get("thread_id"),
            "from": msg.get("from"),
            "to": msg.get("to"),
            "subject": msg.get("subject"),
            "timestamp": msg.get("timestamp"),
            "body": msg.get("body"),
        }
        result = apply_soft_delete(state, {"message_id": req.message_id, "snapshot": snapshot})
        log_decision(
            action="soft_delete",
            reversible=True,
            proposed={"message_id": req.message_id},
            mode="execute",
            human="approved_pending_ui",
            result=result,
            detail=result,
        )
        _append_trace({"event": "pending_soft_delete", "message_id": req.message_id})
    elif action == "archive":
        result = apply_archive(state, {"message_id": req.message_id})
        log_decision(
            action="archive",
            reversible=True,
            proposed={"message_id": req.message_id},
            mode="execute",
            human="approved_pending_ui",
            result=result,
            detail=result,
        )
        _append_trace({"event": "pending_archive", "message_id": req.message_id})
    elif action == "escalate":
        result = "held — escalate remains human-gated (no auto-send)"
        _append_trace({"event": "pending_escalate_hold", "message_id": req.message_id})
    else:
        raise HTTPException(400, f"Unsupported action {action}")

    return {
        "status": "ok",
        "action": action,
        "message_id": req.message_id,
        "result": result,
        "pending_count": len(_live_pending(_inbox())),
    }


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    messages = _inbox()
    by_id = _by_id(messages)
    msg = by_id.get(req.message_id) if req.message_id else None

    context_parts: list[str] = []
    cited: list[str] = []
    if msg:
        thr = _thread_for(msg, messages)
        for m in thr[-6:]:
            cited.append(m["id"])
            context_parts.append(
                f"[{m['id']}] from={m.get('from')} subject={m.get('subject')}\n"
                f"{(m.get('body') or '')[:1200]}"
            )
    else:
        for m in messages[:8]:
            cited.append(m["id"])
            context_parts.append(
                f"[{m['id']}] {m.get('subject')} — {(m.get('body') or '')[:200]}"
            )

    prefs = ""
    mem = ROOT / "memory" / "standing_instructions.json"
    if mem.exists():
        prefs = mem.read_text(encoding="utf-8")[:1500]

    slack_snip = ""
    if SLACK_PATH.exists():
        slack_snip = SLACK_PATH.read_text(encoding="utf-8")[:800]

    system = BASE_SYSTEM
    prefs_text = working_context()
    if prefs and prefs_text == "(no facts stored yet)":
        prefs_text = prefs[:1500]
    user = (
        f"Question: {req.question}\n\n"
        f"Email context:\n{chr(10).join(context_parts)}\n\n"
        f"Standing prefs (if any):\n{prefs_text}\n\n"
        f"Slack snippet:\n{slack_snip or '(none)'}"
    )

    uncertain = _prompt_by_mode("uncertain")
    high = _prompt_by_mode("high_stakes")
    prompt_id = None
    reason = None
    blob = " ".join(context_parts).lower() + " " + req.question.lower()
    if _high_stakes(blob) and high:
        user = _fill_template(
            high.get("user_template") or "",
            {
                "subject": (msg or {}).get("subject") or "",
                "from": (msg or {}).get("from") or "",
                "to": (msg or {}).get("to") or "",
                "cc": "",
                "bcc": "",
                "thread": "\n".join(context_parts),
                "prefs": prefs_text,
            },
        ) + f"\n\nUser question: {req.question}"
        prompt_id = high.get("id")
        reason = "high_stakes"
    elif (not msg or len(context_parts) < 1) and uncertain:
        user = _fill_template(
            uncertain.get("user_template") or "",
            {
                "subject": (msg or {}).get("subject") or "",
                "from": (msg or {}).get("from") or "",
                "to": "",
                "cc": "",
                "bcc": "",
                "thread": "\n".join(context_parts) or "(none)",
                "prefs": prefs_text,
            },
        ) + f"\n\nUser question: {req.question}"
        prompt_id = uncertain.get("id")
        reason = "uncertain"

    answer, used_llm = _llm_reply(system, user)
    if not used_llm and msg:
        answer = _fallback_chat(msg, req.question)
    elif not used_llm:
        answer = _fallback_chat(None, req.question)

    return {
        "answer": answer,
        "cited_message_ids": cited[:12],
        "used_llm": used_llm,
        "message_id": req.message_id,
        "prompt_id": prompt_id,
        "reason": reason,
    }


@app.post("/api/calendar/invite")
def calendar_invite(req: InviteRequest) -> dict[str, Any]:
    attendees = req.attendees or [
        "Maya Chen",
        "Jordan Lee",
        "Priya Shah",
        "Alex Rivera",
    ]
    cal = load_calendar() if CALENDAR_PATH.exists() else {
        "owner": OWNER,
        "timezone": "America/Los_Angeles",
        "events": [],
    }
    events = list(cal.get("events") or [])
    event_id = _next_cal_id(events)
    event = {
        "id": event_id,
        "title": req.title,
        "start": req.start,
        "end": req.end,
        "attendees": attendees,
        "status": "tentative",
        "notes": req.notes or "Synthetic invite from inboxHero demo UI",
    }

    to_line = ", ".join(
        f"{a} <{a.lower().replace(' ', '.')}@northstarlabs.com>" for a in attendees
        if a != "Alex Rivera"
    ) or "colleagues@northstarlabs.com"

    related = req.related_message_id or "msg_002"
    outbox_id = f"out_invite_{event_id}"
    proposed = {
        "outbox_id": outbox_id,
        "in_reply_to": related,
        "thread_id": "thr_invite_demo",
        "from": OWNER,
        "to": to_line,
        "subject": f"Invitation: {req.title}",
        "body": (
            f"Hi team,\n\nYou're invited to: {req.title}\n"
            f"When: {req.start} → {req.end}\n"
            f"Attendees: {', '.join(attendees)}\n\n"
            f"{req.notes or 'Please accept if you can join.'}\n\n"
            f"— Alex (via inboxHero demo)\n"
        ),
        "cited_message_ids": [related] if related.startswith("msg_") else [],
        "message_id": related,
        "kind": "calendar_invite",
        "calendar_event": event,
    }

    overlapping = []
    try:
        from calendar_store import events_overlapping

        overlapping = events_overlapping(req.start, req.end)
    except Exception:  # noqa: BLE001
        overlapping = []

    if not req.execute:
        log_decision(
            action="send",
            reversible=False,
            proposed=proposed,
            mode="dry_run",
            human="n/a_dry_run",
            result="previewed",
            detail="calendar invite dry-run (demo UI)",
        )
        return {
            "status": "preview",
            "execute": False,
            "proposed_event": event,
            "proposed_outbox": proposed,
            "overlaps": [{"id": e.get("id"), "title": e.get("title")} for e in overlapping],
            "detail": "Dry-run only — re-submit with execute=true to write calendar.json and outbox/",
        }

    events.append(event)
    cal["events"] = events
    _save_json(CALENDAR_PATH, cal)
    path = write_outbox_queued(proposed)
    log_decision(
        action="send",
        reversible=False,
        proposed=proposed,
        mode="execute",
        human="approved_demo_ui",
        result="queued",
        detail=f"calendar invite staged {path.name}; event {event_id}",
    )
    return {
        "status": "queued",
        "execute": True,
        "event": event,
        "outbox_path": str(path.relative_to(ROOT)),
        "overlaps": [{"id": e.get("id"), "title": e.get("title")} for e in overlapping],
    }


def _run_script(name: str) -> dict[str, Any]:
    script = SCRIPTS / name
    if not script.exists():
        return {"script": name, "ok": False, "error": "missing"}
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        return {
            "script": name,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-800:],
            "stderr_tail": (proc.stderr or "")[-400:],
        }
    except Exception as exc:  # noqa: BLE001
        return {"script": name, "ok": False, "error": str(exc)}


@app.post("/api/simulate")
def simulate(req: SimulateRequest) -> dict[str, Any]:
    """Load (and optionally rebuild) demo artifacts; return feature summary."""
    steps: list[dict[str, Any]] = []
    needed = {
        "dispositions": DISPOSITIONS_PATH,
        "drafts": DRAFTS_PATH,
        "dashboard_data": DASHBOARD_DATA_PATH,
    }
    missing = [k for k, p in needed.items() if not p.exists()]

    if req.rebuild or missing:
        order = [
            "zero_inbox.py",
            "draft_replies.py",
            "run_actions.py",
            "build_dashboard.py",
        ]
        for name in order:
            steps.append(_run_script(name))
        learn = SCRIPTS / "standing_prefs.py"
        if learn.exists():
            try:
                proc = subprocess.run(
                    [sys.executable, str(learn), "learn"],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                steps.append(
                    {
                        "script": "standing_prefs.py learn",
                        "ok": proc.returncode == 0,
                        "returncode": proc.returncode,
                        "stdout_tail": (proc.stdout or "")[-400:],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                steps.append({"script": "standing_prefs.py learn", "ok": False, "error": str(exc)})

    # Fresh demo: all Primary messages unread
    _clear_read_state()

    dash = _load_json(DASHBOARD_DATA_PATH, {}) or {}
    caps = _load_json(CAPABILITIES_PATH, {}) or {}
    messages = _inbox()
    spam = _spam_list(messages)
    drafts = _drafts_list(messages)
    outbox = _outbox_list()
    pending = _live_pending(messages)
    stats = dash.get("stats") or {
        "pending_count": len(pending),
        "flagged_count": len(dash.get("flagged") or []),
        "commitment_count": len(dash.get("commitments") or []),
        "conflict_count": sum(
            1 for c in (dash.get("commitments") or []) if c.get("conflict")
        ),
    }
    stats = {**stats, "pending_count": len(pending)}

    features = []
    for c in caps.get("capabilities") or []:
        features.append(
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "tier": c.get("tier"),
                "claim": c.get("claim"),
            }
        )

    # Prefer live pending in the landing ticker
    dash_for_log = {**dash, "pending": pending}
    action_log = _action_log(steps, messages, dash_for_log)

    return {
        "loaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rebuilt": bool(req.rebuild or missing),
        "was_missing": missing,
        "steps": steps,
        "action_log": action_log,
        "summary": {
            "message_count": len(messages),
            "pending_count": len(pending),
            "flagged_count": stats.get("flagged_count", len(dash.get("flagged") or [])),
            "spam_count": len(spam),
            "draft_count": len(drafts),
            "outbox_count": len(outbox),
            "commitment_count": stats.get(
                "commitment_count", len(dash.get("commitments") or [])
            ),
            "conflict_count": stats.get("conflict_count", 0),
            "calendar_events": len(
                (_load_json(CALENDAR_PATH, {"events": []}) or {}).get("events") or []
            ),
            "read_cleared": True,
            "artifacts_ready": {
                "inbox": INBOX_PATH.exists(),
                "dispositions": DISPOSITIONS_PATH.exists(),
                "drafts": DRAFTS_PATH.exists(),
                "dashboard_data": DASHBOARD_DATA_PATH.exists(),
                "calendar": CALENDAR_PATH.exists(),
                "outbox": OUTBOX_DIR.exists(),
            },
        },
        "features": features,
        "note": (
            "Assignment R6 calendar lives in dashboard.html after build_dashboard.py. "
            "This UI Commitments tab is the interactive demo (X4)."
        ),
    }


# Static app
if APP_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(APP_DIR), html=True), name="app")


def main() -> None:
    host = os.environ.get("INBOXHERO_HOST", "127.0.0.1")
    port = int(os.environ.get("INBOXHERO_PORT", "8765"))
    url = f"http://{host}:{port}/app/"

    # Fail fast if something else (e.g. python -m http.server) owns the port
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        in_use = probe.connect_ex((host, port)) == 0
    finally:
        probe.close()
    if in_use:
        raise SystemExit(
            f"Port {port} is already in use.\n"
            f"Stop the other process (often `python3 -m http.server {port}`) then retry:\n"
            f"  lsof -iTCP:{port} -sTCP:LISTEN\n"
            f"  kill <PID>\n"
            f"Or run: INBOXHERO_PORT=8770 python3 demo.py --cap X4\n"
            f"Open only the FastAPI URL — static http.server has no /api/* routes."
        )

    print(f"inboxHero demo UI → {url}")
    print("API check: GET /api/bootstrap must return JSON (not an HTML 404 page).")
    print("R6 static dashboard remains: python3 scripts/build_dashboard.py → dashboard.html")
    if os.environ.get("INBOXHERO_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
