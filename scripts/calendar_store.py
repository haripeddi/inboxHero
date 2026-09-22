#!/usr/bin/env python3
"""Load calendar.json for CrewAI tools / conflict checks."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CALENDAR_PATH = ROOT / "calendar.json"


def load_calendar(path: Path | None = None) -> dict[str, Any]:
    p = path or CALENDAR_PATH
    if not p.exists():
        raise FileNotFoundError(f"Missing {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def list_events(path: Path | None = None) -> list[dict[str, Any]]:
    return list(load_calendar(path).get("events") or [])


def events_overlapping(
    start_iso: str,
    end_iso: str,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return events that overlap [start, end)."""
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    hits: list[dict[str, Any]] = []
    for ev in list_events(path):
        es = datetime.fromisoformat(ev["start"])
        ee = datetime.fromisoformat(ev["end"])
        if start < ee and end > es:
            hits.append(ev)
    return hits


def is_slot_free(start_iso: str, end_iso: str, path: Path | None = None) -> bool:
    busy = [
        e
        for e in events_overlapping(start_iso, end_iso, path)
        if e.get("status") != "free"
    ]
    return len(busy) == 0
