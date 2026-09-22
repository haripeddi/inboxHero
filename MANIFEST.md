# inboxHero corpus manifest

## What this system processes

- **Message count:** 100 (exact)
- **Source file:** `inbox.json` (top-level JSON array)
- **Mailbox owner:** Alex Rivera, VP of Product, Northstar Labs (Pulse analytics)
- **Owner address:** `alex.rivera@northstarlabs.com`
- **Time window:** 2026-09-03 through 2026-09-16 (UTC)

## Data format assumptions

Each element is one object with exactly these keys:

| Key | Type | Notes |
|---|---|---|
| `id` | string | Stable id `msg_001` … `msg_100`, assigned after sorting by timestamp |
| `thread_id` | string | Shared across replies in a conversation (`thr_*`) |
| `from` | string | `"Display Name <email@domain>"` |
| `to` | string | One recipient, or comma-separated recipients for Reply-All. Private side-replies in the same `thread_id` use a single address. No separate `cc`/`bcc` fields |
| `subject` | string | Plain text; replies may use `Re:` |
| `timestamp` | string | ISO-8601 UTC, e.g. `2026-09-10T14:15:00Z` |
| `body` | string | Plain text only (no HTML). Multi-message thread replies include Outlook-style quoted history (`On … wrote:` / `>` lines) so one email carries prior replies |
| `unread` | boolean | Mixed read/unread |

Additional assumptions:

- The file is sorted ascending by `timestamp` (then `thread_id`, then `subject` for ties).
- There is no attachments field; attachments are referenced in prose only.
- Some messages only make sense in thread context; triage / RAG should assemble by `thread_id` before acting. Replies also embed prior plain text as quotes for single-message retrieval.
- Within a conversation, `to` may change message-to-message (Reply-All to the group vs Reply to one person) while `thread_id` stays the same.
- Receipts / billing noise keep their own singleton `thread_id`s (realistic; not attached to product threads).
- Display names and domains are fictional; lookalike domains may appear.

## Thread structure (RAG-oriented)

Designed so retrieval can pull full conversation context for a **VP of Product** inbox:

- **15 multi-message conversation threads** (2+ messages sharing a `thread_id`), covering executive scenarios (promo sign-off, $20k tooling, churn escalation, migration vs launch, board prep, headcount, vendor renewal, incident RCA, retention comp, GTM, partner API, all-hands, EOL, cross-team blocker) plus `thr_prefs`
- **4 long threads with exactly 10 messages each:** `thr_churn`, `thr_incident`, `thr_gtm`, `thr_blocker`
- Remaining multi-message threads (3–4 msgs): prefs, promo, budget, migration, board, headcount, vendor renewal, retention, partner, all-hands, EOL
- Cold partnership pitch is a singleton (`thr_cold_pitch`) routed via standing vendor prefs
- Each reply body grows with quoted prior messages (newest prior first), matching real mail clients
- Long / multi-party threads mix Reply-All (`to` with multiple addresses) and private replies (`to` = one person), same `thread_id`
- Singleton noise / phishing / injection / receipts fill the rest to **100** total messages

## Standing preferences (embedded in the corpus)

These are stated in an early owner confirmation thread (not a separate config file):

1. Cold vendor pitches → decline and route to `vendors@northstarlabs.com`.
2. Prefer async updates from eng leads over meetings when possible.
3. Never auto-approve spend over $5k — escalate to CFO Priya Shah.
4. **VIP internal:** Maya Chen (CEO), Jordan Lee (VP Eng) — respond personally / same day.
5. **VIP customers:** Sam Okonkwo (Acme Health), Dana Whitfield (CEO, Acme Health), Marcus Bell (CEO, Brightline Retail), Elena Vos (CRO, Cascade Bank) — high priority, do not auto-delegate.

## Dimension coverage (counts only)

Seeded deliberately; **individual message ids for adversarial cases are not listed here**.

| Dimension | Present in corpus |
|---|---|
| Thread-dependent answers | Yes (budget $20k, partner Option A, long incident/GTM/blocker threads) |
| Time commitment (meeting / deadline) | Yes |
| Ambiguous / ask-don’t-guess | Yes (retention bonus; churn commit vs workaround) |
| Standing-preference triggers | Yes (vendor, budget, VIP) |
| Urgent customer action | Yes (Acme churn threat, API outage RCA — long threads) |
| VIP customer leadership (CEO/CRO) | Yes (Dana/Sam Acme; Elena Cascade in incident) |
| Phishing / social engineering | Yes (≥2) |
| Instruction addressed to an AI reader | Yes (≥1, unlabeled in subjects) |
| Long thread with buried mid-thread ask | Yes (Acme sprint commit mid `thr_churn`) |
| Pure noise (newsletters, receipts, alerts) | Yes (remainder after conversation threads) |

## Part 2 — Zeroing (dispositions)

Every message receives **exactly one** disposition and a stated reason. Vocabulary:

| Disposition | Meaning |
|---|---|
| `reply` | Owner should respond to one person |
| `reply_all` | Owner should respond to the full recipient set on the message |
| `archive` | No further action; clear from the inbox |
| `defer` | Real ask, but not today (time-box later) |
| `delegate` | Hand off to a named owner or alias |
| `escalate` | Needs higher authority (e.g. CFO / CEO path) |
| `mark_as_spam` | Phishing or social engineering |
| `move_to_folder` | File under a named folder (`Receipts`, `Newsletters`, `Vendors`, `Alerts`) — folder named in `reason` |

### Output: `dispositions.json`

Top-level object:

| Key | Type | Notes |
|---|---|---|
| `message_count` | number | Must equal inbox size (100) |
| `rules_only_count` | number | Messages disposed with `route: "rules"` (never required a model call) |
| `model_count` | number | Messages disposed with `route: "model"` |
| `dispositions` | array | One object per message |

Each disposition object:

| Key | Type | Notes |
|---|---|---|
| `id` | string | Matches `inbox.json` message `id` |
| `disposition` | string | One of the vocabulary values above |
| `reason` | string | Short human-readable justification |
| `route` | string | `"rules"` or `"model"` |

### Rules vs model

- **Rules** (deterministic, no model): phishing/lookalike domains, cold vendor pitches, receipts/billing bots, newsletters/digests/calendar/automated alerts, closed prefs acknowledgements, owner-outbound already sent.
- **Model**: everything else (VIP/urgent customer work, spend escalation, ambiguous asks, buried thread asks, preference-sensitive decisions). Local judgment runs by default; optional LLM if `OPENAI_API_KEY` is set.

Run:

```bash
python3 scripts/zero_inbox.py
```

Coverage gate: the run fails if any message lacks a disposition.

## Part 3 — Grounded drafts (RAG)

Some messages can only be answered from an earlier message (same thread or another). This stage retrieves evidence from the mail store and drafts **only** what that evidence supports.

### Retrieval method

**Name:** `hybrid_thread_walk_plus_vector`

1. **Thread walk** — load every message sharing the target’s `thread_id` (chronological).
2. **Vector search** — query the per-message vector index for additional / cross-thread context.
3. **Merge / dedupe** by message `id`, then draft or abstain.

### Vector index

- **Unit:** one embedding per message object (`msg_001` … `msg_100`).
- **Embedded text:** subject + from/to + `body_top` (newest reply only; quoted history stripped so replies are not double-indexed).
- **Backend:** local TF-IDF by default; uses `sentence-transformers` (`all-MiniLM-L6-v2`) when installed.
- **Artifacts:** `indexes/inbox_vectors/embeddings.npy` + `meta.json` (gitignored; rebuild locally).

### Output: `drafts.json`

| Key | Notes |
|---|---|
| `retrieval_method` | Always `hybrid_thread_walk_plus_vector` |
| `drafts` | Array of draft / abstain records |

Each draft record:

| Key | Notes |
|---|---|
| `target_id` | Message being answered (or `unanswerable_demo`) |
| `status` | `drafted` or `insufficient_evidence` |
| `draft_body` | Reply text, or `null` if abstaining |
| `cited_message_ids` | Ids actually used; each must exist in `inbox.json` and appear in the retrieve set |
| `retrieval` | `thread_ids_read` + `vector_hit_ids` |
| `notes` / `reason` | Human-readable grounding or abstain reason |

**Hard rules:** citing an unread / nonexistent id fails the run. Inventing a detail found nowhere in retrieved bodies fails the run. If the information is not in the inbox, the system says so and drafts nothing.

Run:

```bash
python3 scripts/build_index.py
python3 scripts/draft_replies.py
```

## Part 4 — Irreversible action gates

A draft can be rewritten and an archive undone, but a sent message cannot be unsent. Every irreversible action is gated with **dry-run (default)** and **explicit human approval per action** when executing.

### Reversible vs irreversible

| Action | Class | Why |
|---|---|---|
| Rewrite draft | reversible | Local draft only; rewrite anytime |
| Archive / unarchive | reversible | State flag in `mailbox_state.json` |
| Defer / move_to_folder / mark_as_spam (folder move) | reversible | Folder membership is undoable |
| Soft delete (to trash) | reversible | Message retained under trash; restore returns it |
| Restore from trash | reversible | Inverse of soft delete |
| Cancel queued send (within undo window) | reversible helper | Only while outbox status is `queued` |
| **Send** (commit after undo window) | **irreversible** | Outbox file becomes `sent`; cannot unsend after 5s |
| **Permanent delete / purge trash** | **irreversible** | Removes trash entry; content not recoverable in this design |

**Deleting in this design:** soft delete is reversible because the message snapshot is kept in `mailbox_state.json` trash. Permanent delete is irreversible because that retained copy is discarded (and `inbox.json` is never mutated as a recovery source for agent actions).

### Gates

1. **Dry-run (default)** — prints exactly what would happen; writes the decision log only; does **not** write `outbox/` or mutate mailbox state for irreversible outcomes.
2. **Human approval** — `--execute` prompts `y`/`n` per action (`--yes` auto-approves for demos; still logs each decision).

### Send → `outbox/` only

Sending writes **one JSON file per message** under `outbox/`, and nowhere else (no SMTP, no `inbox.json` mutation).

| Key | Notes |
|---|---|
| `outbox_id` | e.g. `out_msg_064` |
| `in_reply_to` | Source inbox message id |
| `thread_id` / `from` / `to` / `subject` / `body` | Outbound message |
| `cited_message_ids` | From grounded draft when applicable |
| `status` | `queued` → (5s undo) → `sent`, or `cancelled` if undone |
| `approved_at` / `undo_until` / `committed_at` | Timestamps |

### Mailbox state

`mailbox_state.json` holds reversible mutations: `archived`, `trash` (soft-deleted snapshots), `purged` (ids permanently removed). Never mutates `inbox.json`.

### Decision log

Append-only `logs/gated_decisions.jsonl` — one JSON object per gated decision:

| Key | Notes |
|---|---|
| `ts` | When logged |
| `action` | `send`, `soft_delete`, `permanent_delete`, `archive`, … |
| `reversible` | boolean |
| `proposed` | Exact payload that would run |
| `mode` | `dry_run` or `execute` |
| `human` | `approved` / `denied` / `n/a_dry_run` / `cancelled_during_undo` |
| `result` | `previewed` / `applied` / `written_queued` / `sent` / `cancelled` / `skipped` / `error` |
| `detail` | Short note |

### Run

```bash
python3 scripts/run_actions.py                 # dry-run (default)
python3 scripts/run_actions.py --execute       # per-action y/n
python3 scripts/run_actions.py --execute --yes # auto-approve; still logs
```

## Part 5 — Standing instructions (persistent memory)

When the owner states a preference, the system **records** it in a file-backed store (SkyValut-style `remember` / `recall`). After the process fully exits and restarts, a later run loads that store and **changes how messages are handled**.

### Store

- **Module:** `scripts/memory.py` (adapted from Assignment 5 / SkyValut `memory.py`)
- **File:** `memory/standing_instructions.json` — `facts` dict; same key overwrites (last write wins)
- Each fact: `key`, `value`, `source`, `updated_at`

### Named preference this system honours

| Field | Value |
|---|---|
| **Key** | `never_auto_approve_spend_over` |
| **Value** | `5000; escalate to Priya Shah <priya.shah@northstarlabs.com>` |
| **Stated in** | `msg_002` (owner triage prefs confirmation) |
| **Affects** | **`msg_064`** — “approve as discussed” for ProtoForge AI (dollar amount `$20,000` only in an earlier thread message) |

Without the memory file, `apply` does not cite standing memory for `msg_064`. After `learn` + restart, `msg_064` is disposed as **`escalate`** with `honoured_memory_key: never_auto_approve_spend_over`.

Sibling facts (vendor routing, async eng, VIP lists) are also recorded on learn from `msg_002`.

### End-to-end (two process lifetimes)

```bash
python3 scripts/standing_prefs.py clear          # optional reset
python3 scripts/standing_prefs.py learn          # Run 1: record prefs, then exit
python3 scripts/standing_prefs.py apply --id msg_064   # Run 2: cold start; honour memory
```

`learn` writes the store and exits. `apply` must not re-learn; it only loads disk and prints disposition JSON including `honoured_memory_key`.

## Part 6 — The hostile inbox

The corpus contains at least one message with instructions addressed to an assistant (forward mail, quiet delete, conceal from the user). The system discovers these by **content scan**, not hardcoded ids — there may be more than one, including non-obvious baits.

### Detection

- **Method:** `detect_hostile_instruction()` in `scripts/rules.py` — regex for assistant/AI-directed framing plus dangerous verbs (exfil, quiet delete, conceal, auto-approve security, etc.)
- **Disposition:** `mark_as_spam` with reason stating instructions were **refused**
- **Never delete:** hostile messages stay in `inbox.json`; no trash/purge/archive for flagged ids

### Refusal log

Each finding appends one JSONL line to `logs/hostile_refusals.jsonl`:

| Field | Meaning |
|---|---|
| `message_id` | Flagged message |
| `attempted` | Short list of attacker asks (e.g. `forward_mailbox`, `quiet_delete`, `conceal_from_user`) |
| `action` | Always `refused` |
| `disposition` | `mark_as_spam` |
| `detail` | Matched phrase evidence |

### Run summary

`zero_inbox.py` prints a **HOSTILE INBOX** section: ids found, what each tried to do, confirmation that nothing was sent/deleted, and the log path. Silently ignoring attacks is not acceptable.

### Action gate

`run_actions.py` refuses soft delete, permanent delete, archive, or outbox send that would act on hostile mail or fulfill an exfil ask. Part 4 demo soft-delete uses receipt noise (`msg_004`), not hostile mail.

### Run

```bash
python3 scripts/zero_inbox.py          # triage + HOSTILE summary + refusal log
python3 scripts/run_actions.py         # dry-run; refuses hostile-target actions
python3 scripts/run_actions.py --execute --yes
```

## Part 7 — The dashboard

One view from a completed run: a reproducible static HTML page generated from JSON (not hand-assembled).

### Outputs

| File | Role |
|---|---|
| `dashboard_data.json` | Structured three-pane payload |
| `dashboard.html` | Self-contained page with that JSON embedded |

Built by `scripts/build_dashboard.py` from `inbox.json`, `dispositions.json`, `drafts.json`, `propose_actions` (Part 4), `mailbox_state.json` / `outbox/`, and `logs/hostile_refusals.jsonl`.

### Three panes

1. **Pending actions** — Part 4 gated proposals the system still wants but has **not yet applied** (`send`, `soft_delete`, `permanent_delete`, `archive`). Each row: message, proposed action, and why a human is required. Already-executed actions (`sent` / `purged` / `archived`) are omitted; an empty pane means nothing is awaiting approval.
2. **Flagged** — Hostile refusals (Part 6), phishing/`mark_as_spam`, and drafts with `insufficient_evidence`. Each row: what was attempted, what the system did instead.
3. **Commitments** — Dates/deadlines/meetings extracted from the inbox, shown as a calendar week strip plus a cited list.

### Commitment rules

- Every commitment has `cited_message_ids` checked against the mail store (build fails on a missing cite).
- At least one commitment derives from **more than one** message (e.g. board date in one message + talking-points / slide lock in another on `thr_board`).
- Overlapping meetings are marked `conflict: true` with `conflicts_with` (seeded pair: promo committee Thu 11:00am vs launch risk sync Thu 11:00am). Conflicts are called out in the UI, not silently listed.
- Relative times use message timestamps; omitted timezones default to **America/Los_Angeles**.

### `dashboard_data.json` shape (summary)

```json
{
  "generated_at": "…Z",
  "message_count": 100,
  "pending": [{ "message_id", "subject", "proposed_action", "why_human", "status", "reversible" }],
  "flagged": [{ "id", "kind", "attempted_summary", "system_did" }],
  "commitments": [{ "id", "title", "kind", "when_start", "when_end", "cited_message_ids", "conflict", "conflicts_with" }],
  "calendar_days": [{ "date", "label", "commitment_ids", "has_conflict" }],
  "stats": { "pending_count", "flagged_count", "commitment_count", "multi_message_commitments", "conflict_count" }
}
```

### Run

```bash
python3 scripts/zero_inbox.py
python3 scripts/draft_replies.py   # needs index from build_index.py
python3 scripts/run_actions.py     # dry-run; leave mailbox/outbox unset so Pending shows the queue
python3 scripts/build_dashboard.py
# open dashboard.html
```

**Interactive demo (X4, not a substitute for R6):** `python3 demo.py --cap X4` (or `python3 scripts/serve_app.py`) serves a white Gmail-like UI at `/app/` with Agent Mode On landing, disposition-derived Pending actions with citations, Reply/Reply-all/CC/BCC **Send** to `outbox/`, learn-from-edit toasts, and provider-agnostic chat via `config.py`. Graded calendar view remains the static Commitments pane in `dashboard.html`.

## Part 8–9 — CrewAI capabilities + pitch

Part 8 agents (`cap_mark_read.py`, `cap_soft_delete_noise.py`, `cap_agent_reply.py`) use a **CrewAI** Agent/Task/Crew design over `inbox.json` + `slack.json` + `calendar.json`. LLM provider is selected through **`config.py`** / `.env.example` (default Ollama `llama3.2`; OpenAI and Gemini supported). Graded entrypoint: `python3 demo.py --cap …`. Pitch files: `CAPABILITIES.md`, `capabilities.json` (R1–R6 + X1–X4). Submission zip should include `outbox/` and root `trace.jsonl` from a full run.
