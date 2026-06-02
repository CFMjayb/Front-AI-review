# Chief of Staff — Design

> A persistent, cross-channel assistant for Jay Bentzen (EDOM) that tracks open
> loops, briefs him daily, and answers questions across his communication tools.
>
> Status: **design / pre-build**. This document is the thing we iterate on before
> writing code.

## 1. Decisions locked

| Decision | Choice |
| --- | --- |
| Interaction model | **Both** — a scheduled daily briefing *and* an on-demand conversational interface |
| Channels (v1) | **Front email**, **Outlook + calendar + Teams**, **Zoom meetings** |
| First build target | **Open-loop tracking** (commitments: who's waiting on whom) |
| Codebase | **Extend this repo** (reuse `auth`, `claude_client`, `front_client`, MCP + scheduler patterns) |
| Briefing delivery | **Daily email at 06:00**, sent to Jay |
| "Gone quiet" threshold | **36 hours** of silence before an `owed_to_me` loop resurfaces |
| Counterparty scope | **Everyone**, but spam / marketing / unsolicited mail is segregated out of loops and the briefing |
| Storage | **Hybrid** — SQLite is the source of truth, rendered to an **Obsidian vault** Jay reads/edits; edits reconcile back |

Out of scope for v1 (revisit later): QuickBooks/finance loops, Gmail, SharePoint/OneDrive document loops.

## 2. The core idea

Email triage (what EDOM already does) answers *"what came in?"*. A chief of staff
answers *"what's still on me, and what am I waiting on?"* — across every channel,
persisted over time. That ledger of **open loops** is the spine of the whole tool.
The daily briefing reads from it; the chat interface reads and edits it.

Crucially, `modules/analyze.py` already emits the raw material per conversation:
`open_questions`, `action_items`, `requires_reply`, `parties`, `deadline`, plus the
message direction. We don't need a new extractor for Front — we need to **persist**
that output with a *direction* and *status*, and then add the same extraction for
Outlook/Teams/Zoom.

## 3. Architecture

Two layers, deliberately separated.

```
                 ┌──────────────────────────────────────────────┐
                 │  Layer 2: SURFACES                            │
                 │   • Daily briefing (scheduled)                │
                 │   • Conversational tools (CoS MCP server)     │
                 └───────────────────┬──────────────────────────┘
                                     │ reads / edits
                 ┌───────────────────▼──────────────────────────┐
                 │  Layer 1: MEMORY (hybrid store)               │
                 │   SQLite (source of truth): loops, people,    │
                 │     profile  ── render ──▶ Obsidian vault      │
                 │   Obsidian vault (you read/edit on phone) ──┐ │
                 │     reconcile edits back ◀──────────────────┘ │
                 └───────────────────▲──────────────────────────┘
                                     │ writes (upserts)
                 ┌───────────────────┴──────────────────────────┐
                 │  INGESTION — channel sweeps → loop extraction │
                 │   Front (front_client) · Outlook · Teams ·    │
                 │   Zoom   (via MCP tools available to agent)   │
                 └──────────────────────────────────────────────┘
```

### Why a ledger + agent (not one big batch script)

Outlook, Teams, and Zoom reach *this* environment as **MCP tools bound to the
agent session**, not as Python SDKs with their own service credentials. Front is
the only channel the standalone pipeline can hit directly today. So the design
that works *now*, with no new credentials, is:

- **Layer 1 (ledger)** is a plain library + an MCP server. It owns persistence and
  has zero channel knowledge. Buildable and testable immediately.
- **Ingestion** is an *agent procedure*: on a schedule (or on demand) an agent
  sweeps each channel using whatever tools it has — `front_client` directly for
  Front, MCP tools for Outlook/Teams/Zoom — extracts commitments, and writes them
  to the ledger via `cos_upsert_loop`. This is the Codex/OpenClaw "agent with
  tools + durable memory" pattern.

This keeps each channel pluggable: adding QuickBooks loops later means teaching the
ingestion agent one more sweep, not rewriting storage or surfaces.

### Storage: hybrid SQLite + Obsidian vault

SQLite is the **source of truth** for the loop ledger — it gives reliable
update-in-place, dedupe, and filtered queries. On top of it, code renders a
**human-readable Obsidian vault** (`vault/`, synced through this repo) so Jay can
read and edit everything on phone or desktop:

- **Loops** → SQLite is authoritative; code *renders* one markdown note per loop
  (and grouped index notes) into `vault/loops/`. Each note carries YAML frontmatter
  (`id`, `direction`, `status`, `due_at`, `snooze_until`). A **reconcile** pass
  reads the vault back, and any frontmatter Jay changed (e.g. `status: done`,
  `snooze_until: …`, or a corrected category) is written back into SQLite. SQLite
  wins on machine-updated fields (last_activity); the vault wins on Jay's manual
  edits. Conflicts resolved by `last_modified` timestamps.
- **Memory** (people, priorities, voice, project/meeting notes) → the **vault is
  authoritative** because it's human-curated. The `people`/`profile` tables are a
  cache refreshed from the vault on reconcile, for fast briefing lookups.

The vault is plain files under git, so it survives the ephemeral container and Jay
keeps a local synced copy (Obsidian Git plugin or Obsidian Sync). All vault writes
go through one renderer module and all reads through one reconciler — the rest of
the system never parses markdown directly.

## 4. Data model (`data/cos.db`, SQLite — mirrored to `vault/`)

```sql
-- One row per open commitment / unanswered thread.
CREATE TABLE loops (
  id            TEXT PRIMARY KEY,   -- stable hash(channel, source_ref, direction)
  direction     TEXT NOT NULL,      -- 'i_owe' | 'owed_to_me'
  counterparty  TEXT NOT NULL,      -- name or email of the other side
  summary       TEXT NOT NULL,      -- "Send the vestry the Q3 budget draft"
  channel       TEXT NOT NULL,      -- 'front'|'outlook'|'teams'|'zoom'
  source_ref    TEXT NOT NULL,      -- conv/message/meeting id
  source_link   TEXT,               -- deep link back to the item
  status        TEXT NOT NULL,      -- 'open'|'waiting'|'snoozed'|'done'|'dropped'
  confidence    REAL,               -- extractor confidence 0..1
  due_at        TEXT,               -- ISO date or NULL
  first_seen    TEXT NOT NULL,      -- ISO timestamp
  last_activity TEXT,               -- ISO timestamp of latest message in thread
  last_reviewed TEXT,               -- when ingestion last touched this row
  snooze_until  TEXT,               -- ISO; hidden from briefing until then
  notes         TEXT
);

-- Lightweight memory about recurring counterparties.
CREATE TABLE people (
  key         TEXT PRIMARY KEY,     -- normalized email
  name        TEXT,
  role        TEXT,                 -- 'bishop','vendor','clergy',...
  importance  INTEGER,              -- 1..5, biases briefing ordering
  notes       TEXT
);

-- Single-row free-form memory: Jay's priorities, voice, standing instructions.
CREATE TABLE profile (
  key   TEXT PRIMARY KEY,           -- 'priorities','voice','standing_orders'
  value TEXT
);
```

**Direction logic.** Per thread we know the last message's sender and whether a
reply is required:

- Last inbound is from the counterparty **and** `requires_reply` / has an open
  question → `i_owe` (they're waiting on Jay).
- Jay sent the last message asking something **and** it's been quiet ≥ 36 h
  (`QUIET_THRESHOLD_HOURS`, default 36) → `owed_to_me` (Jay is waiting on them).
- Zoom action items map to `i_owe` when the owner is Jay, `owed_to_me` otherwise.

**Idempotency.** `id = hash(channel + source_ref + direction)`. Re-sweeping the
same thread *updates* the row (status, last_activity) instead of duplicating it.
A thread that gets a reply flips `i_owe`→resolved or updates `last_activity`.

**Noise segregation.** Scope is everyone, but spam / marketing / unsolicited mail
must not create loops or clutter the briefing. Ingestion classifies each thread
(reusing `analyze.py`'s `category` — `spam`, plus a `solicited` boolean) and:

- never creates a loop for `spam` / marketing / unsolicited threads;
- records them only as a daily count (`briefing` shows "N marketing/spam filtered");
- keeps a `noise` flag on any borderline row so Jay can correct it, feeding the
  existing corrections loop.

## 5. Surfaces

### 5a. CoS MCP server (`cos_mcp_server.py`)
Mirrors `mcp_server.py` (FastMCP, same API-key middleware). Tools:

- `cos_list_loops(direction?, channel?, status?, overdue_only?)` — query the ledger.
- `cos_upsert_loop(...)` — ingestion writes here; agent can too.
- `cos_resolve_loop(id, status)` — mark `done`/`dropped`/`snoozed`.
- `cos_snooze_loop(id, until)` — hide until a date.
- `cos_brief(window='today')` — return the assembled briefing payload (loops due,
  overdue, gone-quiet, plus today's calendar pulled live).
- `cos_remember(key, value)` / `cos_people_upsert(...)` — write to memory.

This is what makes it **conversational**: from Claude Code or chat, Jay asks
"what do I owe people this week?" and the agent calls `cos_list_loops`.

### 5b. Daily briefing (`cos/briefing.py`, scheduled)
Assembles a markdown brief, written like the existing `digest.py`:

1. **Top of mind** — overdue `i_owe` loops, ranked by counterparty importance + age.
2. **Waiting on others** — `owed_to_me` loops gone quiet past threshold.
3. **Today's calendar** — pulled live from Outlook at briefing time, with relevant
   loops attached to each meeting ("prep: you owe Fr. Lee the agenda").
4. **New since yesterday** — loops first seen in the last 24h.
5. **Filtered** — one line: count of spam/marketing/unsolicited set aside today.
6. **Closing note.**

**Delivery: a single email to Jay every day at 06:00.** The brief is written to
`data/briefings/<date>.md` (matches digest) and emailed. Since it's addressed to
Jay himself, this is the one outward send that is *auto-sent* rather than drafted —
the human-in-the-loop rule still holds for anything sent to third parties. Send
mechanism is a config choice (Front channel send, Outlook/Graph, or SMTP);
defaults to whichever channel already holds a verified sending address.

### 5c. Scheduler hook
Add to `scheduler.py`: ingestion sweep every N hours, briefing email daily at 06:00.

## 6. Build sequence

- **M1 — Ledger + CoS MCP server (open-loop tracking).** SQLite schema, ledger
  module, MCP tools, idempotent upsert. *Testable on Front today, no new creds.*
- **M2 — Vault render + reconcile.** Renderer writes loop notes (YAML frontmatter)
  into `vault/loops/`; reconciler reads Jay's edits back into SQLite. One module
  each; conflict rule = machine fields from DB, manual fields from vault.
- **M3 — Front loop extraction.** Extend `analyze.py` output → `cos_upsert_loop`,
  with direction logic. Backfill from recent processed conversations.
- **M4 — Daily briefing assembler** reading the ledger; emailed 06:00 via scheduler.
- **M5 — Cross-channel ingestion.** Channel-agnostic extractor core; Outlook routed
  into Front as an inbox (config, no code); Teams agent-driven via `ms_ingest`.
  Calendar context for the briefing follows in a later pass.
- **M6 — Zoom ingestion**: meeting transcripts → action items → loops.
- **M7 — Memory & voice**: vault-authored people/priorities/voice notes feed
  briefing ranking and draft tone; fold in the existing corrections feedback loop.

## 7. Principles carried over from EDOM

- **Drafts, never auto-sends.** Every outward write is reviewed by Jay.
- **Cost control.** Reuse the one-review gate; ingestion reuses existing analysis
  output rather than re-billing Claude per thread.
- **Human-in-the-loop corrections** train the ledger and memory over time.

## 8. Resolved details

1. **Briefing delivery** — a single email to Jay every day at **06:00** (auto-sent;
   self-addressed, so it does not violate the no-auto-send-to-others rule).
2. **"Gone quiet" threshold** — **36 hours** of silence (`QUIET_THRESHOLD_HOURS`).
3. **Counterparty scope** — **everyone**, with spam / marketing / unsolicited mail
   classified out of the ledger and shown only as a filtered count in the briefing.

### Remaining to decide during M3/M4 (not blocking M1)

- Which verified address/channel actually *sends* the 06:00 email (Front vs Outlook
  vs SMTP).
- Whether "unsolicited but legitimate" (e.g. a first-time real vendor) should create
  a low-priority loop or stay filtered.
