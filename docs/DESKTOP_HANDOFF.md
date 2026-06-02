# Desktop Handoff — Continue from Claude Code on Desktop

This file is the durable record for moving work from Claude Code on the web
(cloud) to Claude Code on the desktop. Everything needed to continue lives in
this repo — clone it, follow the steps, and pick up where the cloud left off.

> Why the move: the cloud environment's network egress allowlist blocks
> `api2.frontapp.com`, so live Front calls 403 at the proxy. The desktop has no
> allowlist, so the real Front validation and pipeline runs actually work there.

---

## 1. One-time desktop setup

```bash
# Clone (skip if you already have the repo)
git clone https://github.com/CFMjayb/Front-AI-review.git
cd Front-AI-review

# Lead branch — all the latest chief-of-staff work
git checkout claude/chief-of-staff-tool-peRoo
git pull

# Virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### Configure secrets

```bash
cp .env.example .env
```

Fill in `.env`. Minimum to run anything that calls Claude or Front:

| Variable | Required? | Notes |
|----------|-----------|-------|
| `FRONT_API_TOKEN` | **Yes** | The `e64a…` token. Authenticates fine; it just couldn't reach Front from the cloud. |
| `ANTHROPIC_API_KEY` | **Yes** | Was unset in the cloud. Needed even for `--dry-run` (the Claude client is built before any Front call). |
| `COS_OWNER_EMAILS` | For CoS | All of your identities (M365 / diocese / CFM) so inbound/outbound direction is correct. Defaults are pre-filled. |
| `COS_OWNER_NAMES` | For CoS | Display names — Teams identifies senders by name, not email. |
| `COS_TIMEZONE` | For CoS | `America/New_York`. Calendars arrive UTC; briefing bounds the day in this zone. |
| `INBOX_IDS` / `TEAMMATE_IDS` | For full pipeline | Comma-separated Front IDs to service. Leave blank for single-conversation runs. |
| `MCP_API_KEY` | For MCP servers | Shared secret (`X-API-Key`) for `mcp_server.py` / `cos_mcp_server.py`. |

All other knobs (`LOOKBACK_DAYS`, `DRY_RUN`, `MAX_RUN_COST_USD`, `BRIEFING_DELIVERY`,
etc.) have working defaults in `.env.example`.

---

## 2. Start Claude Code on the desktop

From inside the repo directory, on the right branch:

```bash
cd Front-AI-review
claude
```

A fresh `claude` session starts with **no memory of the cloud conversation** —
this file plus the design docs below are the handoff. Mention you're continuing
the chief-of-staff work and point Claude at `docs/chief-of-staff/DESIGN.md`.

---

## 3. First thing to run — the validation that was blocked in the cloud

```bash
# Read-only: GET conversation + messages, no writes, no Front mutations.
python cli.py single cnv_1h6cfst6 --dry-run
```

`cnv_1h6cfst6` is a live conversation in the "EDOM Jay's emails" Front inbox
(`inb_cv4ii`). On the desktop this exercises the real Front read path. A clean
run confirms both `FRONT_API_TOKEN` and `ANTHROPIC_API_KEY` are wired correctly.

Smoke test with no creds needed:

```bash
python cli.py help
```

Run the test suite:

```bash
pytest -q
```

---

## 4. Where the projects stand

### Branches
- **`claude/chief-of-staff-tool-peRoo`** — lead branch. 14 commits ahead of
  `main`. All chief-of-staff work + the latest `front_client` 403 fix. **Work here.**
- **`claude/relaxed-wozniak-5ynFZ`** — trails chief-of-staff by one commit
  (missing only the 403 fix). Treat chief-of-staff as the source of truth.
- Open draft PR: **#1** (chief-of-staff → main).

### Two efforts in this repo
1. **EDOM AI Email Ops** (original) — per-conversation AI review pipeline.
   One review per conversation, gated by the `edom-ai/processed` tag.
   - Entry: `cli.py` (`pipeline` / `single`), `scheduler.py`, `mcp_server.py`.
   - Logic: `pipeline.py`, `modules/` (`analyze.py` is the consolidated
     M1–M7 review; `m8_draft.py`, `m4_cluster.py`, `corrections.py`).
2. **Chief of Staff** (open-loop tracking) — built on top of the above.
   - Design: `docs/chief-of-staff/DESIGN.md` and `INGESTION.md`.
   - Code: `cos/` — `ledger.py` (SQLite source of truth, `data/cos.db`),
     `extract.py` / `front_extract.py` (open-loop extraction),
     `ms_ingest.py` (Outlook/Teams via MCP), `calendars.py`,
     `briefing.py` (daily brief assembler), `sender.py` (Front transport).
   - Entry: `cos_mcp_server.py`; briefing scheduled 06:00 via `scheduler.py`.
   - Examples of intended output: `docs/chief-of-staff/examples/`.

### Open design questions (from DESIGN.md)
1. Briefing delivery: Front comment / email draft / file? (`BRIEFING_DELIVERY`)
2. "Gone quiet" threshold (default 36h, `QUIET_THRESHOLD_HOURS`).
3. Track loops with everyone, or only above an importance bar?

---

## 5. Channel access note

The existing pipeline hits **Front directly** over HTTPS (needs network +
`FRONT_API_TOKEN`). Outlook / Teams / Zoom reach the agent only as **MCP tools
bound to the Claude session**, not as direct API calls. The CoS design keeps the
persistent ledger (buildable with no new creds) separate from the ingestion
agent that fills it using whatever tools each channel exposes.

---

## 6. Before you stop a desktop session

The desktop is your machine, so files persist — but to keep collaborators and
future sessions in sync, commit and push:

```bash
git add -A
git commit -m "…"
git push -u origin claude/chief-of-staff-tool-peRoo
```
