#!/usr/bin/env python3
"""
inboxHero graded entrypoint.

Usage:
  python3 demo.py --cap R1
  python3 demo.py --cap X3
  python3 demo.py --list

Every capabilities.json command is reachable through --cap.
Model provider is configured via environment variables loaded in config.py
(see .env.example). Do not put secrets in this file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402

CAPABILITIES_PATH = ROOT / "capabilities.json"

# Fallback map when capabilities.json is missing command fields
FALLBACK_CAPS: dict[str, list[str]] = {
    "R1": [sys.executable, "scripts/zero_inbox.py"],
    "R2": [sys.executable, "scripts/draft_replies.py"],
    "R3": [sys.executable, "scripts/run_actions.py"],
    "R4": [
        sys.executable,
        "scripts/standing_prefs.py",
        "learn",
        "&&",
        sys.executable,
        "scripts/standing_prefs.py",
        "apply",
        "--id",
        "msg_064",
    ],
    "R5": [sys.executable, "scripts/zero_inbox.py"],
    "R6": [sys.executable, "scripts/build_dashboard.py"],
    "X1": [sys.executable, "scripts/cap_mark_read.py"],
    "X2": [sys.executable, "scripts/cap_soft_delete_noise.py", "--execute"],
    "X3": [sys.executable, "scripts/cap_agent_reply.py"],
    "X4": [sys.executable, "scripts/serve_app.py"],
}


def _load_cap_commands() -> dict[str, str]:
    if not CAPABILITIES_PATH.exists():
        return {}
    data = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for row in data.get("capabilities") or []:
        cid = str(row.get("id") or "").upper()
        cmd = str(row.get("command") or "").strip()
        if cid and cmd:
            out[cid] = cmd
    return out


def _run_shell_command(cmd: str) -> int:
    """Run a capabilities.json command string from project root."""
    # Prefer demo.py --cap indirection: if command already is demo.py, unwrap once
    print(f"[demo] LLM provider: {app_config.llm_summary()}")
    print(f"[demo] Running: {cmd}")
    # Use shell so && chains (R4) work
    return subprocess.call(cmd, shell=True, cwd=str(ROOT))


def _run_cap(cap_id: str) -> int:
    cap_id = cap_id.strip().upper()
    commands = _load_cap_commands()
    cmd = commands.get(cap_id)
    if cmd:
        # Avoid infinite recursion if capabilities.json points at demo.py --cap
        if cmd.startswith("python3 demo.py --cap") or cmd.startswith(
            f"{sys.executable} demo.py --cap"
        ):
            # Resolve to underlying script via FALLBACK
            argv = FALLBACK_CAPS.get(cap_id)
            if not argv:
                print(f"Unknown capability {cap_id}", file=sys.stderr)
                return 2
            if "&&" in argv:
                # R4 special-case
                return _run_shell_command(
                    "python3 scripts/standing_prefs.py learn && "
                    "python3 scripts/standing_prefs.py apply --id msg_064"
                )
            print(f"[demo] LLM provider: {app_config.llm_summary()}")
            print(f"[demo] Running: {' '.join(argv)}")
            return subprocess.call(argv, cwd=str(ROOT))
        # Strip leading "python3 demo.py --cap XX && " if present — use as-is
        # If command references demo.py for this same cap, use FALLBACK
        if f"demo.py --cap {cap_id}" in cmd:
            argv = FALLBACK_CAPS.get(cap_id)
            if argv and "&&" not in argv:
                print(f"[demo] LLM provider: {app_config.llm_summary()}")
                print(f"[demo] Running: {' '.join(argv)}")
                return subprocess.call(argv, cwd=str(ROOT))
        return _run_shell_command(cmd)

    argv = FALLBACK_CAPS.get(cap_id)
    if not argv:
        print(f"Unknown capability {cap_id}. Try --list", file=sys.stderr)
        return 2
    if "&&" in argv:
        return _run_shell_command(
            "python3 scripts/standing_prefs.py learn && "
            "python3 scripts/standing_prefs.py apply --id msg_064"
        )
    print(f"[demo] LLM provider: {app_config.llm_summary()}")
    print(f"[demo] Running: {' '.join(argv)}")
    return subprocess.call(argv, cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="inboxHero capability runner")
    parser.add_argument(
        "--cap",
        help="Capability id: R1–R6 or X1–X4",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List capability ids and commands",
    )
    args = parser.parse_args()
    if args.list or not args.cap:
        commands = _load_cap_commands()
        print(f"LLM provider (config.py): {app_config.llm_summary()}")
        print("Capabilities:")
        for cid in ["R1", "R2", "R3", "R4", "R5", "R6", "X1", "X2", "X3", "X4"]:
            cmd = commands.get(cid) or " ".join(
                a for a in FALLBACK_CAPS.get(cid, []) if a != "&&"
            )
            print(f"  {cid}: {cmd}")
        if not args.cap:
            if not args.list:
                parser.print_help()
            return 0 if args.list else 2
    return _run_cap(args.cap)


if __name__ == "__main__":
    raise SystemExit(main())
