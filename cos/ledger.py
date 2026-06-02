"""Chief-of-Staff open-loop ledger — backend facade.

The ledger is the single persistence chokepoint: surfaces (briefing, CoS MCP
server) and ingestion call these functions; nothing else knows the storage.

Two interchangeable backends with identical signatures:
  - LEDGER_BACKEND=sqlite    (default) — local file, used for dev + tests + desktop
  - LEDGER_BACKEND=firestore           — serverless, durable, used in Cloud Run

This module just re-exports the active backend, so callers keep importing
`cos.ledger` and the public API/return shapes are unchanged. Swap backends with
one env var; SQLite stays the instant rollback.
"""
import os

_BACKEND = os.environ.get("LEDGER_BACKEND", "sqlite").lower()

if _BACKEND == "firestore":
    from cos import ledger_firestore as _b
else:
    from cos import ledger_sqlite as _b

# ── Public API (same names in both backends) ─────────────────────────────────
now_iso = _b.now_iso
loop_id = _b.loop_id
init_db = _b.init_db

# Loops
upsert_loop = _b.upsert_loop
get_loop = _b.get_loop
get_loop_by_num = _b.get_loop_by_num
list_loops = _b.list_loops
resolve_loop = _b.resolve_loop
snooze_loop = _b.snooze_loop
resolve_by_num = _b.resolve_by_num
snooze_by_num = _b.snooze_by_num
stats = _b.stats

# People & memory
people_upsert = _b.people_upsert
list_people = _b.list_people
remember = _b.remember
get_memory = _b.get_memory

# Seen gate
was_seen = _b.was_seen
mark_seen = _b.mark_seen

# Calendar events
upsert_event = _b.upsert_event
list_events_between = _b.list_events_between
list_events_overlapping = _b.list_events_overlapping
delete_events_before = _b.delete_events_before

backend_name = _BACKEND
