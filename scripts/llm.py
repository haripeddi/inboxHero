#!/usr/bin/env python3
"""Provider-agnostic chat completions (Ollama | OpenAI | Gemini) via config.py."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config as app_config  # noqa: E402


def resolve_llm_config() -> dict[str, str]:
    return app_config.resolve_llm()


def llm_available() -> bool:
    cfg = resolve_llm_config()
    if cfg["provider"] in {"openai", "gemini"}:
        return bool(cfg.get("api_key"))
    try:
        base = cfg.get("base_url", "http://localhost:11434").rstrip("/")
        with urllib.request.urlopen(f"{base}/api/tags", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ollama_complete(system: str, user: str, cfg: dict[str, str]) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": cfg["model"],
        "stream": False,
        "messages": messages,
        "options": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{cfg.get('base_url', 'http://localhost:11434').rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return ((data.get("message") or {}).get("content") or "").strip()


def _openai_complete(system: str, user: str, cfg: dict[str, str]) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        return ""
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def _gemini_complete(system: str, user: str, cfg: dict[str, str]) -> str:
    model = cfg["model"]
    key = cfg["api_key"]
    text = user if not system else f"{system}\n\n{user}"
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.2},
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    if not parts:
        return ""
    return (parts[0].get("text") or "").strip()


def complete(system: str, user: str) -> tuple[str, bool]:
    """
    Return (text, used_llm).

    On provider failure returns a short error string and used_llm=False.
    """
    cfg = resolve_llm_config()
    provider = cfg["provider"]
    try:
        if provider == "openai":
            text = _openai_complete(system, user, cfg)
        elif provider == "gemini":
            text = _gemini_complete(system, user, cfg)
        else:
            text = _ollama_complete(system, user, cfg)
        if text:
            return text, True
        return f"(empty response from {provider})", False
    except Exception as exc:  # noqa: BLE001
        return (
            f"(LLM unavailable — {provider}: {exc})",
            False,
        )


def llm_complete(prompt: str, system: str = "") -> str:
    """Single-string helper used by CrewAI tool fallbacks."""
    text, _ = complete(system or "You are a helpful email agent.", prompt)
    return text


def build_crewai_llm():
    """Return a CrewAI / compat LLM bound to the configured provider."""
    from crewai_compat import get_crewai

    _, _, _, _, LLM, _, _ = get_crewai()
    cfg = resolve_llm_config()
    if cfg["provider"] == "openai":
        return LLM(model=cfg["crew_model"], temperature=0.2)
    if cfg["provider"] == "gemini":
        # Official crewai may accept gemini/; compat path routes via llm.complete
        return LLM(model=cfg["crew_model"], temperature=0.2)
    return LLM(
        model=cfg["crew_model"],
        base_url=cfg.get("base_url", "http://localhost:11434"),
        temperature=0.2,
    )


# Back-compat alias
ollama_available = llm_available
