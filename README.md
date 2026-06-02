# EDOM AI Email Ops (Python)

AI-assisted email operations for the Episcopal Diocese of Maryland. Mirrors the structure of `front-mail-organizer` for consistency.

## Cost-control rule

Every conversation gets exactly ONE AI review. After processing, the pipeline applies an `edom-ai/processed` tag. Future runs check this tag first and skip already-processed conversations before any Claude call. To re-review, manually remove the tag in Front.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt

cp .env.example .env
# fill in FRONT_API_TOKEN and ANTHROPIC_API_KEY

# Smoke test
python cli.py help

# Single conversation, dry run
python cli.py single <CONV_ID> --dry-run

# Live single
python cli.py single <CONV_ID>

# Full pipeline (currently M1 only — M2-M8 to follow)
python cli.py pipeline
```

## Status

**Minimal viable scope:** scaffold + M1 (classification) + pipeline gate + CLI. Once the `edom-ai/processed` gate is validated against a real conversation, M2-M8, scheduler, digest, and MCP server will follow.

## Architecture

- `front_client.py` — Front API wrapper (extends `front-mail-organizer/front_client.py` with tags, drafts, teammates, search).
- `claude_client.py` — Anthropic SDK wrapper with prompt caching.
- `auth.py` — secret resolution (env var → GCP Secret Manager fallback). Same pattern as `front-mail-organizer`.
- `pipeline.py` — orchestrator. Fetches 5 sources, gates on `edom-ai/processed`, runs modules, tags on success.
- `modules/m1_classify.py` — first AI module (others to follow).
- `cli.py` — command-line entry: `pipeline`, `single`, `help`.

## Chief of Staff (in progress)

A cross-channel open-loop tracker + daily briefing built on top of the email
pipeline. Design: [`docs/chief-of-staff/DESIGN.md`](docs/chief-of-staff/DESIGN.md).

**M1 (done):** the open-loop ledger and its MCP server.

- `cos/ledger.py` — SQLite ledger (source of truth) for loops, people, memory.
  Loops are idempotent on `channel + source_ref + direction`; a manually-set
  status (done/dropped/snoozed) is never overwritten by a re-sweep.
- `cos_mcp_server.py` — exposes the ledger as `cos_*` tools (list/upsert/resolve/
  snooze loops, stats, people, memory). Same FastMCP + X-API-Key pattern as
  `mcp_server.py`. Run: `python cos_mcp_server.py` (HTTP) — defaults to port 8081.
- Tests: `python -m pytest tests/test_ledger.py`

Next: M2 vault render/reconcile, M3 Front loop extraction, M4 daily briefing.
