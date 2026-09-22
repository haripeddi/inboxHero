#!/usr/bin/env python3
"""
CrewAI-compatible Agent/Task/Crew runtime powered by Ollama Llama.

Used when the full `crewai` package cannot be installed (e.g. Python 3.14 or
arch mismatch). API mirrors CrewAI enough for inboxHero Part 8 agents.
Prefer `import crewai` when available — see get_crewai().
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


def _ollama_chat(messages: list[dict[str, str]], model: str, base_url: str) -> str:
    payload = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("message") or {}).get("content", "").strip()


@dataclass
class LLM:
    model: str = "ollama/llama3.2"
    base_url: str = "http://localhost:11434"
    temperature: float = 0.2

    def call(self, messages: list[dict[str, str]]) -> str:
        name = self.model or ""
        # Route OpenAI / Gemini through shared llm.complete (config.py)
        if name.startswith("gpt-") or name.startswith("openai/") or name.startswith("gemini"):
            try:
                from llm import complete

                system = ""
                user_parts: list[str] = []
                for m in messages:
                    role = m.get("role") or "user"
                    content = m.get("content") or ""
                    if role == "system":
                        system = content
                    else:
                        user_parts.append(content)
                text, _ = complete(system, "\n".join(user_parts))
                return text
            except Exception as exc:  # noqa: BLE001
                return f"(LLM unavailable: {exc})"
        if name.startswith("ollama/"):
            name = name.split("/", 1)[1]
        return _ollama_chat(messages, name, self.base_url)


def tool(name_or_fn: str | Callable | None = None):
    """Decorator compatible with crewai.tools.tool."""

    def deco(fn: Callable) -> Callable:
        fn._is_tool = True  # type: ignore[attr-defined]
        fn.name = getattr(fn, "__name__", "tool")  # type: ignore[attr-defined]
        if isinstance(name_or_fn, str):
            fn.name = name_or_fn  # type: ignore[attr-defined]
        fn.description = (fn.__doc__ or fn.name).strip()  # type: ignore[attr-defined]
        return fn

    if callable(name_or_fn):
        return deco(name_or_fn)
    return deco


@dataclass
class Agent:
    role: str
    goal: str
    backstory: str
    llm: LLM | None = None
    tools: list[Any] = field(default_factory=list)
    verbose: bool = False
    allow_delegation: bool = False

    def run(self, task_prompt: str, context: str = "") -> str:
        llm = self.llm or LLM(
            model=os.environ.get("OLLAMA_MODEL", "llama3.2")
            if not os.environ.get("OLLAMA_MODEL", "").startswith("ollama/")
            else os.environ.get("OLLAMA_MODEL", "ollama/llama3.2"),
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        )
        # Normalize model for LLM dataclass
        if not isinstance(llm, LLM):
            llm = LLM(model=str(llm))
        model = llm.model
        if not model.startswith("ollama/") and "/" not in model:
            llm = LLM(model=f"ollama/{model}", base_url=llm.base_url)

        tool_docs = []
        tool_map: dict[str, Callable] = {}
        for t in self.tools:
            name = getattr(t, "name", getattr(t, "__name__", "tool"))
            desc = getattr(t, "description", getattr(t, "__doc__", "") or "")
            tool_docs.append(f"- {name}: {desc}")
            tool_map[name] = t

        system = (
            f"You are {self.role}. Goal: {self.goal}. Backstory: {self.backstory}. "
            "You may call tools by outputting a single JSON line: "
            '{"tool": "TOOL_NAME", "args": {...}} '
            "When finished, output: {\"final\": \"...your answer...\"}"
        )
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"Tools:\n" + "\n".join(tool_docs) + "\n\n"
                    f"Context:\n{context}\n\nTask:\n{task_prompt}"
                ),
            },
        ]

        last = ""
        for _ in range(6):
            try:
                last = llm.call(messages)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # Offline / Ollama down: run tools deterministically when possible
                return self._deterministic_finish(task_prompt, tool_map, str(exc))

            messages.append({"role": "assistant", "content": last})
            parsed = _extract_json(last)
            if not parsed:
                # Treat free text as final if no JSON
                if "final" in last.lower() or len(last) > 40:
                    return last
                messages.append(
                    {
                        "role": "user",
                        "content": 'Reply with JSON {"tool":...} or {"final":...}',
                    }
                )
                continue
            if "final" in parsed:
                return str(parsed["final"])
            tname = parsed.get("tool")
            args = parsed.get("args") or {}
            if tname in tool_map:
                fn = tool_map[tname]
                try:
                    if args:
                        result = fn(**args) if _accepts_kwargs(fn) else fn(json.dumps(args))
                    else:
                        result = fn()
                except TypeError:
                    # single-string tools
                    result = fn(*args.values()) if args else fn()
                messages.append(
                    {
                        "role": "user",
                        "content": f"Tool {tname} result:\n{result}\nContinue.",
                    }
                )
            else:
                messages.append(
                    {"role": "user", "content": f"Unknown tool {tname}. Use a listed tool or final."}
                )
        return last or self._deterministic_finish(task_prompt, tool_map, "max_steps")

    def _deterministic_finish(
        self, task_prompt: str, tool_map: dict[str, Callable], note: str
    ) -> str:
        bits = [f"(compat mode note: {note})"]
        for name, fn in tool_map.items():
            if name in {
                "list_digest_candidates",
                "list_noise_for_soft_delete",
                "list_calendar_events",
                "recall_standing_prefs",
            }:
                try:
                    bits.append(f"{name}: {fn()}")
                except Exception as exc:
                    bits.append(f"{name} error: {exc}")
        return "\n".join(bits)


@dataclass
class Task:
    description: str
    expected_output: str
    agent: Agent
    context: list[Task] = field(default_factory=list)
    output: str | None = None


class Process:
    sequential = "sequential"


@dataclass
class Crew:
    agents: list[Agent]
    tasks: list[Task]
    process: str = Process.sequential
    verbose: bool = False

    def kickoff(self) -> str:
        ctx = ""
        last = ""
        for task in self.tasks:
            prior = "\n\n".join(t.output or "" for t in (task.context or []) if t.output)
            prompt = (
                f"{task.description}\n\nExpected output: {task.expected_output}"
            )
            last = task.agent.run(prompt, context=prior or ctx)
            task.output = last
            ctx = last
        return last


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    # fenced
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                try:
                    return json.loads(p)
                except json.JSONDecodeError:
                    pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _accepts_kwargs(fn: Callable) -> bool:
    import inspect

    try:
        sig = inspect.signature(fn)
        return any(
            p.kind in (p.VAR_KEYWORD, p.KEYWORD_ONLY) or p.default is not p.empty
            for p in sig.parameters.values()
        )
    except Exception:
        return False


def get_crewai():
    """Return (Agent, Task, Crew, Process, LLM, tool) from real crewai or compat."""
    try:
        from crewai import Agent as A
        from crewai import Crew as C
        from crewai import LLM as L
        from crewai import Process as P
        from crewai import Task as T
        from crewai.tools import tool as tool_fn

        return A, T, C, P, L, tool_fn, "crewai"
    except Exception:
        return Agent, Task, Crew, Process, LLM, tool, "crewai_compat_ollama"
