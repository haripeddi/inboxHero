# Capabilities — inboxHero (Parts 2–9)

Student project: **CrewAI** multi-agent inbox assistant for VP of Product Alex Rivera (Northstar Labs). Local context stores: `inbox.json`, `slack.json`, `calendar.json`, and standing prefs in `memory/standing_instructions.json`.

Graded entrypoint: **`python3 demo.py --cap <R1|…|X4>`** (dispatches the commands below). Model provider is configured only through environment variables loaded by **`config.py`** (see `.env.example`). Do not commit `.env`.

## Framework and model

- **Framework: CrewAI.** Roles (Triage, Context Researcher, Reply Drafter, Action Executor) map cleanly onto inbox work: classify noise, gather cross-channel context, draft, then gate irreversible sends. Tools keep agents grounded in files instead of inventing Slack/calendar facts.
- **Model (provider-agnostic):** default **Ollama `llama3.2`** via `INBOXHERO_LLM=ollama`. Switch with `INBOXHERO_LLM=openai` (+ `OPENAI_API_KEY`) or `INBOXHERO_LLM=gemini` (+ `GEMINI_API_KEY`). All paths go through `config.py` → `scripts/llm.py`. No API key is hardcoded.
- **Retrieval (Parts 2–3):** `hybrid_thread_walk_plus_vector` — thread walk for same-conversation facts, vector index for cross-thread evidence. CrewAI tools call the same stores for Part 8.

## Escalation line (what we will not auto-do)

- **Auto:** mark digests read; soft-delete clear phishing/cold ads (not hostile); VIP (Maya/Jordan) **simple ack** when calendar is free.
- **Hold / escalate:** spend ≥ $5k → Priya; churn sprint commits; legal/GTM sign-off; promo/headcount; anything hostile (Part 6 — flag, never delete).
- **Trade-off:** file-backed Slack/calendar (no live APIs); send only to `outbox/` with Part 4 dry-run + approval; soft-delete reversible via `mailbox_state.json`.

## Required capabilities (R1–R6)

### R1 — Zero the inbox (tier B)
Assign exactly one disposition to every message; rules-first, model path for the rest.  
`python3 demo.py --cap R1` → `dispositions.json` + HOSTILE summary.

### R2 — Grounded drafts / RAG (tier B)
Hybrid retrieve + cite mail-store ids; abstain if evidence missing.  
`python3 demo.py --cap R2` → `drafts.json`.

### R3 — Gated irreversible actions (tier C)
Dry-run + human approval; send → `outbox/` only; log decisions.  
`python3 demo.py --cap R3` (add `--execute` via `python3 scripts/run_actions.py --execute` to apply).

### R4 — Standing preferences (tier C)
Learn prefs to disk; after process restart, escalate spend on `msg_064`.  
`python3 demo.py --cap R4`.

### R5 — Hostile inbox defense (tier B)
Detect assistant-directed attacks; refuse; log; leave in place.  
`python3 demo.py --cap R5` → `logs/hostile_refusals.jsonl`.

### R6 — Run dashboard (tier B)
Three panes: pending, flagged, commitments (multi-cite + conflicts).  
`python3 demo.py --cap R6` → `dashboard.html`.

## Own capabilities (X1–X4) — CrewAI + demo UI

### X1 — Mark digests read (tier A)
CrewAI triage agent marks daily/weekly summaries read.  
`python3 demo.py --cap X1` → `outputs/mark_read.json`.

### X2 — Soft-delete spam / phishing / ads (tier B)
CrewAI cleanup agent batch soft-deletes noise; **refuses** Part 6 hostile ids.  
`python3 demo.py --cap X2` → `outputs/soft_delete_batch.json`.

### X3 — Agentic reply with Slack + calendar (tier C)
CrewAI researcher + drafter: email thread + `slack.json` + `calendar.json` + prefs → draft; auto-stage VIP simple acks or hold.  
`python3 demo.py --cap X3` → `outputs/agent_reply.json`, `logs/cap_x3_trace.jsonl`.

### X4 — Interactive Gmail demo UI (tier B)
White Gmail-like shell: Primary / Pending / Commitments / Flagged / Spam / Drafts / Outbox; **Agent Mode On** landing; Reply / Reply-all / CC / BCC with **Send** (outbox) + learn-from-edit toasts; disposition-derived Pending rows with **citations**; provider-agnostic chat via `config.py`.  
**Does not replace R6** — graders still use `python3 demo.py --cap R6` → `dashboard.html`.  
`python3 demo.py --cap X4` → http://127.0.0.1:8765/app/

## Machine-readable

See `capabilities.json` for the marking-script schema (`student`, `repo`, `system`, `capabilities[]`). Submission zip should also include `outbox/` and root `trace.jsonl` from a full run.
