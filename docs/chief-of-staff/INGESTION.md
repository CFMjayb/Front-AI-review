# Ingestion playbook (per channel)

How loops get into the ledger from each channel. All channels normalize to the
same thread shape and run through `cos/extract.py`, so a loop means the same thing
everywhere.

## Front email — automatic, in-pipeline

No action needed. The existing pipeline analyzes each conversation and
`cos/front_extract.py` turns that analysis into loops, with a Claude-free reconcile
pass closing them as you reply. Runs every `RUN_INTERVAL_HOURS`.

## Outlook email — routed through Front

**Decision:** Outlook/M365 email is ingested by connecting the mailbox to Front as
an inbox, not by separate code. Once connected, Outlook mail is just another Front
inbox and flows through the pipeline above.

Setup:
1. In Front: **Settings → Inboxes → Add inbox → Office 365 / Outlook**, connect the
   mailbox.
2. Find the new inbox's ID (`front_list_inboxes` MCP tool, or the Front URL).
3. Add it to `INBOX_IDS` in `.env`. Done — loops start appearing on the next run.

No `cos` code runs for Outlook; it reuses the Front path end to end.

## Teams chat — agent-driven (`cos/ms_ingest.py`)

Teams has no clean Front equivalent, so an **agent run** fetches it via MCP and
feeds normalized messages to `ms_ingest.ingest()`. The ledger's `seen` table gates
analysis to once per thread state, so repeated runs are cheap.

Recommended cadence: an agent session (e.g. via the scheduling/`loop` mechanism)
runs this a few times a day.

### Agent procedure

1. **Fetch** recent Teams messages with the `chat_message_search` MCP tool over the
   lookback window (e.g. `afterDateTime: "36 hours ago"`). Page via `nextOffset`.
2. **Group** messages by chat (`chat_id`). For each chat, build a list of message
   dicts:

   ```python
   {"sender_email": "...", "sender_name": "...", "recipients": ["..."],
    "ts_epoch": 1733155200, "text": "..."}
   ```

3. **Normalize + ingest** by running this in the repo (it identifies which side is
   Jay from `COS_OWNER_EMAILS`, analyzes once per state, and upserts loops):

   ```python
   import os
   from auth import get_anthropic_api_key
   from claude_client import ClaudeClient
   from cos import ms_ingest

   claude = ClaudeClient(api_key=get_anthropic_api_key(),
                         default_model=os.environ.get("ANTHROPIC_MODEL_ANALYZE", "claude-sonnet-4-6"),
                         fast_model=os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5"))

   threads = [ms_ingest.thread_from_teams(chat_id=cid, messages=msgs, web_link=link)
              for cid, (msgs, link) in chats.items()]
   print(ms_ingest.ingest(threads, claude))   # {analyzed, created, skipped, errored, cost_usd}
   ```

4. **Reconcile** (optional, Claude-free): to auto-close Teams loops once the other
   side replies, fetch the latest message per open Teams loop and call
   `cos.extract.reconcile(ledger.list_loops(channel="teams"), fetch_fn)`.

### Config

- `COS_OWNER_EMAILS` — comma-separated addresses that are "you" (drives inbound vs.
  outbound direction). Falls back to `SENDER_TO`.
- `QUIET_THRESHOLD_HOURS` — silence before an outbound ask becomes `owed_to_me` (36).

## Calendars — agent-driven (`cos/calendars.py`)

Calendars come from the `outlook_calendar_search` MCP tool. An agent sweep pulls
your own + named shared calendars into the `events` cache; the autonomous briefing
reads that cache (no MCP at 06:00) for the 📅 Today section, conflict flags, and
meeting-prep loops.

### Agent procedure

1. **For each calendar** in `cos.calendars.configured_calendars()` (`'self'` plus
   `COS_CALENDARS`), call `outlook_calendar_search` for today (and a few days out):
   - own calendar: omit `calendarOwnerEmail`;
   - a shared one: pass `calendarOwnerEmail=<email>` (or `calendarName`).
   Use `query: "*"`, `afterDateTime: "today"`, `beforeDateTime: "in 2 days"`.
2. **Ingest** the raw events (the normalizer handles Graph shapes):

   ```python
   from cos import calendars
   calendars.ingest_events(own_events, "self")
   calendars.ingest_events(bishop_events, "bishop@episcopalmaryland.org")
   calendars.sync_prep_loops(calendars.events_for_day())   # prep loops for today
   calendars.expire_past_calendar_loops()                  # close finished ones
   ```

The briefing also runs `sync_prep_loops` / `expire_past_calendar_loops` itself, so
it's correct even if the sweep is stale — but a sweep shortly before 06:00 keeps
the day's events fresh.

### Config

- `COS_CALENDARS` — comma-separated extra calendars (owner emails / names).

## Future channels

`ingest()` is channel-agnostic. Any agent-fetched source (Zoom action items in M6,
etc.) just needs a `thread_from_*` adapter that produces the normalized shape.
