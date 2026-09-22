"""
inboxHero runtime configuration.

Model provider and related settings come from environment variables
(optionally loaded from a local `.env` file). Never hardcode API keys.
Copy `.env.example` → `.env` for local overrides; `.env` is gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Best-effort load of ROOT/.env without requiring python-dotenv."""
    path = ROOT / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_dotenv()


def llm_provider() -> str:
    """Return ollama | openai | gemini (default ollama)."""
    return os.environ.get("INBOXHERO_LLM", "ollama").strip().lower() or "ollama"


def ollama_model() -> str:
    model = os.environ.get("OLLAMA_MODEL", "llama3.2").strip() or "llama3.2"
    if model.startswith("ollama/"):
        model = model.split("/", 1)[1]
    return model


def ollama_base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")


def openai_api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"


def gemini_api_key() -> str:
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


def gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"


def resolve_llm() -> dict[str, str]:
    """
    Resolve active LLM settings for agents and the demo UI.

    Preference order when INBOXHERO_LLM is unset/ollama: Ollama.
    Set INBOXHERO_LLM=openai|gemini (with the matching API key) to switch.
    """
    provider = llm_provider()
    if provider == "openai" and openai_api_key():
        model = openai_model()
        return {
            "provider": "openai",
            "model": model,
            "crew_model": model,
            "api_key": openai_api_key(),
        }
    if provider == "gemini" and gemini_api_key():
        model = gemini_model()
        return {
            "provider": "gemini",
            "model": model,
            "crew_model": f"gemini/{model}",
            "api_key": gemini_api_key(),
        }
    # Default / fallback: Ollama
    model = ollama_model()
    return {
        "provider": "ollama",
        "model": model,
        "crew_model": f"ollama/{model}",
        "base_url": ollama_base_url(),
    }


def llm_summary() -> str:
    cfg = resolve_llm()
    return f"{cfg['provider']}/{cfg['model']}"
