# 26-119 — Chief of Staff (Front AI Review)

Cross-channel "open-loop" tracker + daily briefing, built on top of the EDOM AI Email
Ops pipeline. Brought local from Claude Code on the web (cloud) on 2026-06-02.

> The cloud environment's network-egress allowlist blocks `api2.frontapp.com`, so live
> Front calls 403 there. The **desktop has no allowlist**, so real Front validation +
> pipeline runs work here. That's why this was moved local.

---

## Repo / Git

- **GitHub:** `https://github.com/CFMjayb/Front-AI-review.git` (same repo as 26-117/26-118 family)
- **Working branch:** `claude/chief-of-staff-tool-peRoo` — the **lead branch** (14 commits ahead of `main`).
  Treat as source of truth. `claude/relaxed-wozniak-5ynFZ` trails it by one commit.
- Open draft PR **#1** (chief-of-staff → main).
- This is a **real git clone** (has `.git`), unlike the 26-117 folder which is a loose file copy.
- Before stopping a desktop session: `git add -A && git commit -m "…" && git push -u origin claude/chief-of-staff-tool-peRoo`

## Local environment (this PC)

- **Python:** 3.14 venv at `.venv\` (system default `py`; the 3.12 bundle is 26-100-only).
- **Install:** `py -m venv .venv` → `.venv\Scripts\python.exe -m pip install -r requirements.txt`
- **Secrets:** resolved via **GCP Secret Manager**, NOT pasted into `.env`.
  - `.env` has `USE_SECRET_MANAGER=true`, `GCP_PROJECT=cfm-front-mail`.
  - `auth.py` reads `front-api-token`, `anthropic-api-key`, `mcp-api-key`, `analyze-examples`,
    `inbox-ids`, `teammate-ids` from project **`cfm-front-mail`** (also home of `front-mail`
    Artifact Registry + Cloud Run service `front-ai-review`, region `us-east1`).
  - Auth method = **Application Default Credentials**. gcloud installed at
    `%LOCALAPPDATA%\Google\Cloud SDK\…\bin`; ran `gcloud auth application-default login`;
    ADC file at `%APPDATA%\gcloud\application_default_credentials.json` with
    `quota_project_id=cfm-front-mail`.
  - **ADC is per-machine and NOT in OneDrive** — every new PC needs its own
    `gcloud auth application-default login` (one-time). Account needs role
    `roles/secretmanager.secretAccessor` on `cfm-front-mail`.
  - `.env` is gitignored; no plaintext secrets on disk.

## How to run

```bash
.venv\Scripts\activate
python cli.py help                              # smoke test, no creds
python cli.py single cnv_xxxx --dry-run         # read-only single conversation
python cli.py single cnv_xxxx                    # live single (writes tag/draft)
python cli.py pipeline [--dry-run]              # full pipeline across sources
python -m pytest -q                              # 60 tests
python cos_mcp_server.py                         # CoS ledger MCP server (HTTP :8081)
```

## Two efforts in this repo

1. **EDOM AI Email Ops** (original) — per-conversation AI review. ONE review per
   conversation, gated by the `edom-ai/processed` tag (cost control). Entry: `cli.py`,
   `scheduler.py`, `mcp_server.py`. Logic: `pipeline.py`, `modules/` (`analyze.py` is the
   consolidated M1–M7 review; `m8_draft.py`, `m4_cluster.py`, `corrections.py`).
2. **Chief of Staff** (open-loop tracking) — built on top. Design in
   `docs/chief-of-staff/DESIGN.md` + `INGESTION.md`. Code in `cos/`:
   `ledger.py` (SQLite source of truth, `data/cos.db`), `extract.py`/`front_extract.py`
   (open-loop extraction), `ms_ingest.py` (Outlook/Teams via MCP), `calendars.py`,
   `briefing.py` (6 AM daily brief), `sender.py` (Front transport). Entry: `cos_mcp_server.py`;
   briefing scheduled 06:00 in `scheduler.py`; output to `data/briefings/`.

Channel access: Front is hit **directly** over HTTPS (needs `FRONT_API_TOKEN`).
Outlook/Teams/Zoom reach the agent only as **MCP tools bound to the Claude session**.

## Open design questions (from DESIGN.md)

1. Briefing delivery: Front comment / email draft / file? (`BRIEFING_DELIVERY`, default `file`)
2. "Gone quiet" threshold (default 36h, `QUIET_THRESHOLD_HOURS`).
3. Track loops with everyone, or only above an importance bar?

## Current State (2026-06-05)

### Architecture
Three Cloud Run **Jobs** + one Cloud Run **Service**, all in GCP project `cfm-front-mail`:
- **edom-pipeline** (job) — runs every 4h via Cloud Scheduler; fetches Front conversations, tags them, extracts CoS loops into Firestore
- **edom-briefing** (job) — runs daily at 06:00; reads Firestore ledger, sends briefing email to `jay@cfmins.org` via Front channel `cha_gcc4a`
- **edom-digest** (job) — runs Mondays at 07:00; weekly digest
- **front-ai-review** (service) — always-on MCP server; exposes CoS tools to Claude Code sessions

Storage: **Firestore** (production) via `LEDGER_BACKEND=firestore`. SQLite stays for local dev/tests.
`cos/ledger.py` is a facade that dispatches to `cos/ledger_firestore.py` or `cos/ledger_sqlite.py`
based on `LEDGER_BACKEND` env var. Swap backends with one env var.

### Local Clone
- Branch: `claude/chief-of-staff-tool-peRoo` (fully synced to origin/main as of 2026-06-05)
- Venv: `.venv\` (Python 3.14).
- ADC: expires periodically — `Run CoS Triage.bat` auto-refreshes via browser. Per-machine, not in OneDrive.
- `LEDGER_BACKEND` not in `.env` — defaults to sqlite for local; set env var manually for Firestore queries:
  `$env:LEDGER_BACKEND="firestore"; $env:GCP_PROJECT="cfm-front-mail"`

### Firestore Ledger — Current State (2026-06-05)
- **~327 active loops** after triage + dedup cleanup
- All loops backfilled: urgency, action_type, source_date, sentiment, dedup_key populated
- 4 sender rules seeded: @hq.bill.com, @bill.com, @atlanticunionbank.com, @plaud.ai → FYI
- 4 guidance records seeded: wire-confirmations, parish-payment-questions, grant-awards, bill-com-fyi

### Loop Schema (as of 2026-06-05)
All loops now carry: `num`, `direction`, `counterparty`, `counterparty_email`, `summary`, `channel`, `source_ref`, `source_link`, `category`, `fyi`, `status`, `importance`, `confidence`, `due_at`, `source_date`, `urgency`, `action_type`, `sentiment`, `escalation_risk`, `suggested_assignee`, `deferred`, `dedup_key`, `first_seen`, `last_activity`, `last_reviewed`, `notes`, `snooze_until`

### Triage Tool — Current State (2026-06-05)
- **Export:** 15-column redesign + Sender Rules tab + Guidance tab. Filename now `CoS Triage YYYY-MM-DD HH-MM.xlsx` (timestamped). Locked-file → clean error message.
- **Import:** Handles Triage sheet + Sender Rules tab + Guidance tab edits. Auto-finds latest dated file.
- **Run CoS Triage.bat:** ADC auth check with auto-browser-reauth on expiry; single "press any key" at end.
- **Pipeline:** DEDUP-1 live (dedup_key per loop, skips duplicate sender+subject). FILTER-1/PRIORITY-1 live (sender_rules Firestore collection, pipeline checks before Claude). GUIDANCE-1 live (guidance injected into analyze.py prompt per call).
- **analyze.py:** Comment only posted when action required (reply/approval/payment/action items) — prevents spurious conversation date bumps on informational emails.

### Inbox Coverage (as of 2026-06-05)
- `inbox-ids` secret: `inb_cv4ii,inb_csx96` (EDOM Jay's emails + Jay's personal work email)
- Was missing `inb_csx96` since June 3 fix — restored 2026-06-05

### Cloud Run Job Environment Variables (as of 2026-06-05)
**edom-pipeline:** GCP_PROJECT=cfm-front-mail, USE_SECRET_MANAGER=true, LEDGER_BACKEND=firestore, EARLIEST_DATE=2026-04-01, SOURCE_MAX_PAGES=5, DRY_RUN=false, MAX_RUN_COST_USD=10
**edom-briefing:** LEDGER_BACKEND=firestore, GCP_PROJECT=cfm-front-mail, USE_SECRET_MANAGER=true, BRIEFING_DELIVERY=email, SENDER_TRANSPORT=front, SENDER_FRONT_CHANNEL_ID=cha_gcc4a, SENDER_TO=jay@cfmins.org
**edom-digest:** GCP_PROJECT=cfm-front-mail, USE_SECRET_MANAGER=true, LEDGER_BACKEND=firestore

### Process Standards
- **Data migration rule:** Every data model change requires backfill + verification in the same session.
- **Output verification rule:** Spot-check actual cell values and field counts before declaring done.

## Next Steps

1. **ENTITY-1** — entity field on loops; needs Front inbox IDs mapped to entity codes (InboxMap tab in QBOcompanies.xlsx). Prerequisite: confirm inbox ID → entity mapping with Jay.
2. **Twilio setup** — add TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM/TO secrets to GCP for Positive Pay SMS alerts
3. **Teammate-ids** — confirm whether `tea_byq3e` should stay in scope
4. **inb_csx96 backlog** — first pipeline run after inbox fix will catch up; monitor for unexpected volume
5. Remaining roadmap: M2 vault, M6 Zoom, M7 memory/voice
