# Simple file-backed memory. Facts survive across program runs.
# Conflict rule: same key overwrites the old value (last write wins).
# Adapted from SkyValut/memory.py for inbox standing instructions.

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEMORY_DIR = ROOT / "memory"
MEMORY_FILE = MEMORY_DIR / "standing_instructions.json"

# Cap how many facts we put into a context summary.
MAX_CONTEXT_FACTS = 40

# Named Part 5 preference (also documented in MANIFEST.md)
SPEND_PREF_KEY = "never_auto_approve_spend_over"


def _normalize_key(key: str) -> str:
    key = str(key).strip().lower()
    key = key.replace(" ", "_")
    return key


def _empty_store() -> dict[str, Any]:
    return {"facts": {}}


def _load() -> dict[str, Any]:
    path = MEMORY_FILE
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data) is not dict or "facts" not in data:
            return _empty_store()
        if type(data["facts"]) is not dict:
            return _empty_store()
        return data
    except Exception:
        return _empty_store()


def _save(store: dict[str, Any]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


def clear() -> dict[str, Any]:
    """Delete the standing-instructions store (demo reset)."""
    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()
    return {"status": "ok", "action": "cleared", "path": str(MEMORY_FILE)}


def remember(key: str, value: str, source: str) -> dict[str, Any]:
    """Save a fact. If the key already exists, overwrite it."""
    key = _normalize_key(key)
    if key == "":
        return {"status": "error", "message": "key cannot be empty"}

    value = str(value).strip()
    if value == "":
        return {"status": "error", "message": "value cannot be empty"}

    source = str(source).strip()
    if source == "":
        source = "user"

    store = _load()
    overwritten = key in store["facts"]
    store["facts"][key] = {
        "key": key,
        "value": value,
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(store)

    action = "updated" if overwritten else "created"
    return {
        "status": "ok",
        "action": action,
        "key": key,
        "value": value,
        "source": source,
    }


def recall(query: str) -> dict[str, Any]:
    """Find facts whose key or value contains the query text."""
    query = str(query).strip().lower()
    if query == "":
        return {"status": "error", "message": "query cannot be empty"}

    store = _load()
    matches = []
    for key, fact in store["facts"].items():
        key_text = key.lower()
        value_text = str(fact.get("value", "")).lower()
        if query in key_text or query in value_text:
            matches.append(
                {
                    "key": fact.get("key", key),
                    "value": fact.get("value", ""),
                    "source": fact.get("source", ""),
                    "updated_at": fact.get("updated_at", ""),
                }
            )

    if len(matches) == 0:
        return {"status": "ok", "matches": [], "message": "No matching facts found"}

    return {"status": "ok", "matches": matches}


def get_fact(key: str) -> dict[str, Any] | None:
    """Exact-key lookup; returns None if missing."""
    key = _normalize_key(key)
    store = _load()
    fact = store["facts"].get(key)
    if not fact:
        return None
    return {
        "key": fact.get("key", key),
        "value": fact.get("value", ""),
        "source": fact.get("source", ""),
        "updated_at": fact.get("updated_at", ""),
    }


def working_context() -> str:
    """Short bullet list of stored facts."""
    store = _load()
    facts = store["facts"]
    if len(facts) == 0:
        return "(no facts stored yet)"

    lines = []
    count = 0
    for key in sorted(facts.keys()):
        if count >= MAX_CONTEXT_FACTS:
            break
        fact = facts[key]
        lines.append("- " + key + ": " + str(fact.get("value", "")))
        count = count + 1

    text = "\n".join(lines)
    if len(facts) > MAX_CONTEXT_FACTS:
        text = (
            text
            + "\n(more facts exist on disk; call recall if you need something not listed)"
        )
    return text


def parse_spend_pref(value: str) -> tuple[int, str] | None:
    """
    Parse '5000; escalate to Priya Shah <priya...>' → (5000, 'Priya Shah').
    """
    raw = str(value).strip()
    if not raw:
        return None
    threshold = None
    m = re.search(r"(\d[\d,]*)", raw)
    if m:
        threshold = int(m.group(1).replace(",", ""))
    cfo = "Priya Shah"
    m2 = re.search(r"escalate to\s+([^;<]+)", raw, re.I)
    if m2:
        cfo = m2.group(1).strip()
    if threshold is None:
        return None
    return threshold, cfo
