# CoS Cloud Persistence — Firestore (approved 2026-06-02)

## Problem
Cloud Run has no durable disk, so the SQLite ledger (`data/cos.db`) resets every
run. Three surfaces need one shared, persistent ledger:
1. `edom-pipeline` job (every 2h) — extracts + reconciles loops.
2. `edom-briefing` job (6 AM) — reads loops, sends the brief. **Does not exist yet.**
3. `cos_mcp_server` service — live conversational edits (`#73 done`).

## Decision
**Firestore (Native mode)** in project `cfm-front-mail`. Serverless, ~free at
single-user scale, durable, concurrent across all three surfaces, and a *single
source of truth* the desktop and the cloud can both point at. `ledger.py` is the
only module that touches storage, so the change is contained there.

## Design

### Backend flag
`LEDGER_BACKEND = sqlite | firestore` (default `sqlite` for local dev + tests).
`ledger.py` dispatches each public function to the active backend. Public API and
return shapes are unchanged, so callers (briefing, extract, MCP server) are untouched.

### Firestore data model (collections)
| Collection | Doc id | Fields |
|-----------|--------|--------|
| `loops` | loop id `front-<hash>-<dir>` | num, direction, counterparty, counterparty_email, summary, channel, source_ref, source_link, category, status, importance, confidence, due_at, snooze_until, first_seen, last_activity, last_reviewed, notes |
| `people` | normalized email | name, role, importance, notes |
| `memory` | key | value |
| `seen` | `<channel>|<source_ref>` | marker, seen_at |
| `events` | provider event id | calendar, subject, start_at, end_at, location, organizer, attendees[], source_link, is_all_day, updated_at |
| `_counters` | `loops` | next_num |

### Stable `#num`
A transaction on `_counters/loops` increments `next_num` and assigns it on first
insert (atomic; no races). Existing numbers are preserved on update.

### Queries
`list_loops` maps to Firestore `where` filters (direction, channel, status) + the
"hide done/dropped" default; overdue uses `due_at <` now. Ordering
(importance desc, due_at asc) is applied client-side (small N) to avoid composite
indexes initially.

## Work plan
1. **Storage layer:** refactor `ledger.py` into `cos/ledger_sqlite.py` (current code)
   + `cos/ledger_firestore.py`, with `ledger.py` as the dispatcher. Keep the exact
   public signatures. *(SQLite stays default; tests unchanged.)*
2. **Dependency:** add `google-cloud-firestore` to `requirements.txt`.
3. **GCP:** enable Firestore API; create a Native-mode database in `cfm-front-mail`
   (region `us-east1` / `nam5`). Grant the runtime service account
   `roles/datastore.user`.
4. **Briefing job:** add an `edom-briefing` Cloud Run job (entry: `cos.briefing.run_briefing`)
   + a Cloud Scheduler trigger at 06:00 America/New_York. Env: `LEDGER_BACKEND=firestore`,
   `BRIEFING_DELIVERY=email`, sender vars.
5. **Pipeline + MCP:** set `LEDGER_BACKEND=firestore` on the `edom-pipeline` job and a
   (new or existing) `cos-mcp` Cloud Run service.
6. **One-time seed:** push the current desktop ledger (88 loops) into Firestore so the
   first cloud brief is populated.
7. **Deploy:** via the existing GitHub Actions workflow (extend to build/deploy the
   briefing job + cos-mcp service).

## Testing
- SQLite backend keeps the existing unit tests (fast, no network).
- Firestore backend tested against the **Firestore emulator** (`gcloud emulators
  firestore`) in a separate test module, skipped when the emulator isn't running.

## Cost
Firestore free tier (50K reads / 20K writes / 1 GiB per day) far exceeds a single
user's loop volume — effectively $0/mo. No idle cost (serverless).

## Rollback
`LEDGER_BACKEND=sqlite` instantly reverts to the local file. The desktop remains a
working fallback throughout.
