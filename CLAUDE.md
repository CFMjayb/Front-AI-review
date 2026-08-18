# 26-119 — Chief of Staff (Front AI Review)

Cross-channel "open-loop" tracker + daily briefing, built on top of the EDOM AI Email
Ops pipeline. Brought local from Claude Code on the web (cloud) on 2026-06-02.

> The cloud environment's network-egress allowlist blocks `api2.frontapp.com`, so live
> Front calls 403 there. The **desktop has no allowlist**, so real Front validation +
> pipeline runs work here. That's why this was moved local.

---

## Repo / Git

- **GitHub:** `https://github.com/CFMjayb/Front-AI-review.git` (same repo as 26-117/26-118 family)
- **Working branch:** `claude/chief-of-staff-tool-peRoo` — the **lead branch**, kept
  fast-forward-synced with `main`. Verified level with `origin/main` at `12070fc` on
  2026-08-18 (the old "14 commits ahead" note was stale — it is 0 ahead / 0 behind).
  `claude/relaxed-wozniak-5ynFZ` trails it.
- Open draft PR **#1** (chief-of-staff → main).
- This is a **real git clone** (has `.git`), unlike the 26-117 folder which is a loose file copy.
- Before stopping a desktop session: `git add -A && git commit -m "…" && git push -u origin claude/chief-of-staff-tool-peRoo`

## Deploy

CI is **GitHub Actions** (`.github/workflows/deploy.yml`), triggered **only on push to `main`** (or manual `workflow_dispatch`). There is no local deploy script.

- **To deploy:** get commits onto `main` — `git push origin HEAD:main` (the lead branch is kept fast-forward-synced with main). That push triggers the workflow.
- The workflow builds the Docker image tagged with the commit SHA, pushes to Artifact Registry `front-mail`, deploys the **front-ai-review** service, updates all 3 jobs (**edom-pipeline**, **edom-digest**, **edom-briefing**) to the new image, then smoke-tests the service (expects 401/403).
- `gh` CLI is **not installed** on this PC, so `workflow_dispatch` from a branch isn't available here — push-to-main is the path.
- **Verify from this machine:** `python verify_deploy.py` — uses ADC to query the Cloud Run Admin API and confirm the service + 3 jobs report the expected image SHA and the service returns 401/403. Service URI: `https://front-ai-review-2k7f2bz3dq-ue.a.run.app`.
- **Auth note:** gcloud CLI auth (`gcloud auth login`) can expire (`invalid_grant`) **independently** of ADC. ADC (`gcloud auth application-default login`) is what the Python google-cloud libs use — Firestore queries and `verify_deploy.py` keep working even when the gcloud CLI is stale.

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
python -m pytest -q                              # 80 tests
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

### Firestore Ledger — Current State (2026-06-05, project note corrected 2026-07-27)
- **Firestore project is `cfm-qbo-mcp`, NOT `cfm-front-mail`.** `GCP_PROJECT=cfm-front-mail` (Secret Manager) and `FIRESTORE_PROJECT=cfm-qbo-mcp` (the actual CoS ledger DB) are two different GCP projects — set separately in `.env` and in `.github/workflows/deploy.yml`'s Cloud Run env vars for all 3 jobs. `cos/ledger_firestore.py`'s `_db()` reads `FIRESTORE_PROJECT` first, falling back to `GCP_PROJECT` only if unset. Don't assume the two are the same project.
- **~327 active loops** after triage + dedup cleanup
- All loops backfilled: urgency, action_type, source_date, sentiment, dedup_key populated
- 4 sender rules seeded: @hq.bill.com, @bill.com, @atlanticunionbank.com, @plaud.ai → FYI
- 4 guidance records seeded: wire-confirmations, parish-payment-questions, grant-awards, bill-com-fyi

### Front Archive Sync — Current State (2026-06-05)
Bidirectional sync between resolved Firestore loops and Front conversation status — all live + deployed (commit 659f6b9):
- **Firestore resolved → Front archived:** triage import (done/drop/exclude) archives the Front conversation. It GETs current Front status first; if already non-open (archived/spam) or 404, it skips the PATCH and just stamps Firestore. ("Completed" in a team inbox = `archived` at the API level — same call as a personal-inbox archive.)
- **Front archived → Firestore resolved:** the pipeline `reconcile` pass (Claude-free, runs every pipeline run) closes a loop when its Front conversation is archived/deleted/trashed; it now also stamps `front_archived`. The reply-direction-flip branch is intentionally NOT stamped (a reply legitimately leaves the thread open).
- **`front_archived` flag:** boolean on every loop meaning "this Front conversation is archived." Set by triage import, backfill, and reconcile. Makes `backfill_archive_front_resolved.py` **idempotent** — it processes only resolved Front loops where `front_archived` is unset, so re-runs are near-no-ops.
- **Backfill state:** **1,511/1,511** resolved Front loops stamped, 0 stragglers (verified by `poll_front_archived.py`).
- **No re-review of resolved/archived:** `_filter_open` + a `_process_one` status guard skip any non-open conversation; `AI/processed` tag is applied BEFORE loop extraction with a 429 retry, so a mid-write rate-limit can't leave a conversation untagged and re-processed every run.
- **Diagnostics (read-only, kept in repo root):** `poll_front_archived.py` (stamp report), `verify_deploy.py` (Cloud Run image + health via ADC).

### Loop Schema (as of 2026-08-18)
All loops now carry: `num`, `direction`, `counterparty`, `counterparty_email`, `summary`, `channel`, `source_ref`, `source_link`, `category`, `fyi`, `status`, `importance`, `confidence`, `due_at`, `source_date`, `urgency`, `action_type`, `sentiment`, `escalation_risk`, `suggested_assignee`, `deferred`, `dedup_key`, `front_archived`, `mailbox`, `first_seen`, `last_activity`, `last_reviewed`, `notes`, `snooze_until`

`mailbox` added 2026-08-18 — see "Per-Mailbox Split" below.

### Triage Tool — Current State (2026-06-16)
- **CoS Triage Workbook.xlsm** replaces the bat-file export/import workflow. Built via `create_triage_workbook.py` (run `Run CoS Triage Workbook.bat`). 7 VBA modules: modConfig, modApi, modTriage, modSenderRules, modGuidance, modBriefing, modInstall.
- **Three buttons on Triage sheet:** Refresh Triage (GET /api/cos/loops), Save Actions (POST /api/cos/triage per row), Send Briefing (POST /api/cos/briefing → on-demand briefing email).
- **Two additional sheets:** Sender Rules (Refresh + Save), Guidance (Refresh + Save).
- **API key** baked into hidden Config sheet by the build script (fetched from Secret Manager at build time).
- **VBA note:** Private sub/function names must NOT have a leading underscore — VBA compile error when called without `Call` keyword. All private helpers named without underscore prefix.
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

## Current State (2026-07-27) — triage import 403 fixed

- **Root cause:** `cos_triage_import.py`'s Firestore writes go to project `cfm-qbo-mcp` (see `FIRESTORE_PROJECT` note above), authenticated locally via the user-level `GOOGLE_APPLICATION_CREDENTIALS` → `cfm-daily-jobs-sa` key (overrides ADC for every `google.cloud` library call, regardless of what `Run CoS Triage.bat`'s own ADC-freshness check reports). On 2026-07-18, that SA was downgraded `datastore.user` → `datastore.viewer` on `cfm-qbo-mcp` as part of 26-124's Firestore-removal cleanup — verified at the time as safe for `cfm-front-mail`, but nobody accounted for 26-119 actually pointing at `cfm-qbo-mcp` via `FIRESTORE_PROJECT`. Reads (Export) kept working under `datastore.viewer`; writes (Import's patch/resolve/snooze calls) 403'd.
- **Fix:** Jay re-granted `cfm-daily-jobs-sa` `roles/datastore.user` on `cfm-qbo-mcp`. First retry right after the grant showed a real, documented GCP IAM-propagation-lag pattern — 35 of 432 actioned rows succeeded, the rest 403'd, in the same run, using the same identity — resolved by waiting a few minutes and retrying just the failed rows (filtered a copy of the workbook down to only the 397 previously-errored `#` numbers, to avoid double-appending the Notes column on rows that had already succeeded). Second pass: 395/397 succeeded.
- **Unrelated bug found + fixed in the same session:** 3 of those 397 hit `'charmap' codec can't encode character '→'` — same class of bug as 26-103's 2026-06-10 box-drawing-character crash (Windows console can't print the `→` arrow in a Front-API warning message, which crashes mid-print inside `_archive_in_front`'s own exception handler, before it can return `False`). Fixed by adding `set PYTHONUTF8=1` to `Run CoS Triage.bat` (prior version backed up as `Run CoS Triage_prior_pythonutf8_fix.bat`). All 3 affected loops (their ledger status had already resolved to "done" before the crash — only the `front_archived` stamp was skipped) were completed after the fix. **Full import now clean: 0 errors.**

## Current State (2026-08-18) — Per-Mailbox Split (two spreadsheets)

Jay: *"We will be keeping three spreadsheets now instead of one"* → narrowed the
same session to *"let's just have two spreadsheets for this round."*

### The registry is the only place mailboxes are defined
`cos/mailboxes.py` — one mailbox = one or more Front inboxes = one triage
workbook = one section of the morning email. Adding a mailbox there is the whole
change: the pipeline starts scanning its inbox, new loops get stamped, the export
writes another workbook, the briefing grows a section. Nothing else hardcodes a
mailbox.

| key | label | address | Front inbox | scanned | loops (2026-08-18) |
|-----|-------|---------|-------------|---------|--------------------|
| `cfm` | Jay — CFM | jay@cfmins.org | `inb_csx96` | yes | 468 |
| `edom` | Jay — EDOM | jboggs@episcopalmaryland.org | `inb_cv4ii` | yes | 162 |
| `dme` | DME Finance | finance@episcopalmaine.org | `inb_cr72y` | **no** | 28 |
| `other` | Unattributed | — | (anything unregistered) | n/a | 0 |

**Jay's original third address, `jboggs@episcopalmaine.org`, does not exist in
Front** — no inbox, no channel. Verified live. The near-identical
`jboggs@episcopalmaryland.org` is the real one and holds most existing loops.
Don't "restore" the Maine spelling without confirming a mailbox was actually
connected.

### Why `dme` is registered with `scan: False` — read before "fixing" it
An early reading of the config concluded this inbox had **zero** loops, because
it is not in `INBOX_IDS` and therefore not in the inbox scan list. That was
wrong: it has 28. They arrive by a **second, easily-missed route** — the
`teammate-ids` secret is `tea_byq3e` (Jay), so `_fetch_all_sources` also calls
`list_assigned_conversations` and pulls **every conversation assigned to Jay
regardless of which inbox it lives in**. DME finance conversations assigned to
him land in the ledger that way. Local `.env` has `TEAMMATE_IDS=` empty, which is
why this is invisible when reasoning from the local config alone — check the
`teammate-ids` secret, not `.env`.

Consequences worth keeping straight:
- Registering it costs nothing. Those loops are already fetched and already paid
  for; the entry only decides whether they are *labelled* "DME Finance" or dumped
  in the Unattributed bucket. Same file count either way.
- Flipping `scan` to `True` is a genuinely different decision: it would fetch the
  **whole shared finance queue**, not just Jay's assigned items, and pay for AI
  review of all of it. Not done, and not implied by the entry existing.
- More generally: **any** inbox where Jay gets assigned conversations can produce
  loops. That is why the Unattributed bucket exists and why the export creates a
  workbook for it when non-empty rather than dropping those loops.

This also means Jay's ask for a `finance@episcopalmaine.org` spreadsheet was
already backed by real data — it just wasn't labelled. He asked for two
spreadsheets "for this round," but three mailboxes have loops, and the only way to
produce two files is to drop the 28 DME loops out of triage entirely. Three
correctly-labelled workbooks was chosen over two plus silent data loss.

### Attribution rule
A loop belongs to the mailbox whose Front inbox its conversation sits in, asked
of Front directly via `GET /conversations/{id}/inboxes` (new
`FrontClient.list_conversation_inboxes`). **Never inferred from the recipient
list** — a conversation can be addressed to several of Jay's addresses while
living in exactly one inbox. A conversation in two registered inboxes resolves to
the first registry entry, deterministically.

`pipeline.py` stamps `_cos_mailbox` on each conversation dict during the inbox
fetch (the only point it is known for free — `_dedupe_by_id`/`_filter_*` flatten
every source into one list and would otherwise lose it). `_mailbox_for()` falls
back to the live Front lookup for paths with no hint: single-conversation mode
and teammate-assigned conversations.

`upsert_loop` sets `mailbox` once and never blanks it — the Claude-free
`reconcile` pass re-upserts loops without knowing the mailbox, and that must not
wipe attribution (covered by a test).

### What changed
- `cos/mailboxes.py` — new registry.
- `cos/ledger_firestore.py` / `ledger_sqlite.py` — `mailbox` on `upsert_loop`,
  `list_loops(mailbox=…)`, `patch_loop(mailbox=…)`; sqlite migration adds the
  column. The `other` filter matches loops whose field is missing entirely, so an
  unstamped loop surfaces rather than vanishing.
- `cos_triage_export.py` — `export_all()` writes one workbook per mailbox,
  sharing one timestamp: `CoS Triage YYYY-MM-DD HH-MM - <Mailbox>.xlsx`. The date
  stays immediately after "CoS Triage " so the importer's existing glob matches.
- `cos_triage_import.py` — `_find_latest_exports()` returns the whole newest
  **batch**; a blank path imports every mailbox workbook. Importing only one file
  would silently discard the other mailbox's edits.
- `cos/briefing.py` — `gather(mailbox=…)`, `gather_by_mailbox()`, `render_all()`.
  One email, one `## 📬 <Mailbox>` section each, an on-you/waiting index line, and
  **both workbooks attached**. Calendar stays shared (one schedule, not one per
  mailbox). Subject line + `run_briefing()["counts"]` stay all-mailbox totals;
  new `["by_mailbox"]` carries the breakdown.
- `mcp_server.py` — `GET /api/cos/loops?mailbox=<key>`; `mailbox` appended as a
  TSV column. **No workbook rebuild needed** — `modTriage.bas` parses by index
  with `fc >` guards and ignores extra trailing fields. The .xlsm still shows all
  mailboxes mixed; scoping it is a follow-up, not part of this change.
- `cos_mcp_server.py` — `cos_list_loops(mailbox=…)`.
- `backfill_loop_mailbox.py` — new, idempotent. Active + deferred only by
  default; `--include-resolved` opts into the ~1,500 done/dropped loops, which
  never appear in a workbook or the briefing.

### Two real bugs found by testing, not review
1. **`front_client.py` rate-limit crash (pre-existing, affects the nightly
   pipeline too).** Front documents `x-ratelimit-reset` / `Retry-After` as
   integers but sends fractional values — `1787064542.997`. The bare `int()`
   raised `ValueError` from inside the rate-limit handler, turning a routine
   throttle into a failed request. Only fires when the remaining-quota header
   hits zero, i.e. under sustained load. Fixed with `_num_header()`
   (`int(float(v))` + fallback); regression test in `tests/test_front_client.py`.
2. **Empty-mailbox export crash (latent before the split).** With zero loops the
   Triage Action dropdown range computes as `M2:M1`, which openpyxl rejects.
   Never fired when there was always ≥1 loop; a per-mailbox workbook can legitimately
   be empty. Now guarded on `total_rows >= 2`.

Also worth knowing: on a 429 the Front client sleeps `Retry-After` **and then
raises**, so a caller must retry to benefit from the wait. The backfill's first
pass treated that as a hard error and skipped ~2% of loops; it now retries.

### Verification
- `pytest`: 109 pass (93 pre-existing + 12 mailbox + 4 front_client). The 3
  failures are **pre-existing and unrelated** — confirmed identical at `HEAD` by
  stashing: `tests/test_front_extract-CFM2606.py` is a stale untracked duplicate,
  and `tests/test_sender.py` was never updated when `attachments` was added to
  `send()` on 2026-07-30.
- Export verified by loading the generated workbooks back with openpyxl (sheets,
  rows, per-mailbox Instructions header) and by exercising the zero-loop case.
- Backfill run live against production Firestore: **658 loops, 468 cfm / 162 edom
  / 28 dme, zero unattributed, zero unstamped, zero errors.** Verified by querying
  Firestore directly afterwards, not from the script's own summary.
- All three workbooks loaded back with openpyxl: row counts 468/162/28 sum to
  exactly the 658 in the ledger; per-mailbox Instructions header correct; dropdown
  present; `_id` hidden. Importer confirmed to see all three as one batch.
- Morning email sent live (`delivery=http`, 3 attachments) and its rendered
  markdown inspected: index line plus a correctly-scoped section per mailbox.

### Unrelated observation worth chasing later
Every one of the 658 open loops has `direction='i_owe'`; the ledger has **zero
`owed_to_me` loops**, so "Waiting on others" renders empty in every mailbox. That
is pre-existing and untouched by this change, but it means one of the briefing's
four sections is dead weight — either direction detection in
`extract.loop_from_thread` is skewed toward "on you", or nothing outstanding is
genuinely owed to Jay. Worth a look before trusting that section.

### Gotcha for local runs
`.env` sets `COS_DB_PATH=` (empty) with `load_dotenv(override=True)`, so setting
`COS_DB_PATH` as an env var **does not** redirect the local sqlite ledger — a
"scratch" export will read the real `data/cos.db` and write into the real
`data/triage/`. Point tests at a temp DB via the pytest fixture (monkeypatch
before import), not an env var on the command line.

Also: `mailboxes.scan_inbox_ids()` unions the registry with `INBOX_IDS`, so local
runs now scan both inboxes even though `.env` lists only `inb_cv4ii`. The union is
deliberate — an inbox never silently stops being scanned because a deploy's env
var lags this file. `digest.py` (weekly) still reads `INBOX_IDS` directly and was
deliberately left alone.

## Next Steps

1. **Test workbook end-to-end** — Refresh Triage (verify all columns/colors), Save Actions (mark one item done, confirm Firestore update), Send Briefing (confirm email arrives).
2. **ENTITY-1** — entity field on loops; needs Front inbox IDs mapped to entity codes (InboxMap tab in QBOcompanies.xlsx). Prerequisite: confirm inbox ID → entity mapping with Jay. **Note (2026-08-18):** `cos/mailboxes.py` now does inbox → mailbox mapping in code. If ENTITY-1 goes ahead, extend that registry rather than adding a second inbox-mapping table — the split-brain risk is the same one flagged for `user_program_areas` in 26-129.
3. **DME Finance: decide whether to actually scan it.** It is registered and produces a workbook today from Jay's assigned conversations (28 loops). Setting `scan: True` in `cos/mailboxes.py` would additionally pull the whole shared finance queue and pay for AI review of it — Jay has not asked for that. Separately, if he ever wants only two workbooks, the 28 DME loops have to go somewhere; there is no configuration that yields two files without dropping them.
4. **Scope the .xlsm workbook to a mailbox** — the server already accepts `?mailbox=`; the workbook still pulls all mailboxes into one sheet. Needs a Config/Controls selector + VBA rebuild.
5. **Twilio setup** — add TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM/TO secrets to GCP for Positive Pay SMS alerts
6. **Teammate-ids** — confirm whether `tea_byq3e` should stay in scope. **This is now a bigger decision than it looks (2026-08-18):** that secret is the reason loops arrive from inboxes that are not in `INBOX_IDS` at all — it fetches every conversation assigned to Jay regardless of inbox, and it is the sole source of the 28 DME Finance loops. Removing it would silently empty that mailbox's workbook. Any change here should be checked against the mailbox distribution first.
7. **`verify_deploy.py` is broken on this PC (found 2026-08-18)** — it fails with `403 run.jobs.get denied` because ADC resolves to `GOOGLE_APPLICATION_CREDENTIALS` → `cfm-daily-jobs-sa`, which has no Cloud Run read access on `cfm-front-mail`; it also reports a stale "expecting image tag" derived from the wrong ref. Verify deploys with `gcloud run jobs describe <job> --region us-east1 --project cfm-front-mail --format="value(spec.template.spec.template.spec.containers[0].image)"` instead (the gcloud CLI is authenticated as `jay@cfmins.org`). For the *service*, also check `status.latestReadyRevisionName`/`status.traffic[0].revisionName`, not just the spec image — see `feedback_cloud_run_deploy_verification`.
8. **Uncommitted drift in this folder** — `create_triage_workbook.py`, `VBA/`, `CoS Triage Workbook.xlsm`, `poll_front_archived.py`, `verify_deploy.py`, `recover_failed_plaud_loops.py` and several `*-CFM2606.py` / `*_pre_*.py` copies are untracked and have never been committed. None are in the Docker image's runtime path so the deploy is unaffected, but this is the same orphaned-work pattern the root CLAUDE.md warns about. Decide per file: commit, or move to a clearly-marked archive folder.
9. Remaining roadmap: M2 vault, M6 Zoom, M7 memory/voice
