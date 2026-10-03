# 26-119 Chief of Staff: closed loops not archived in Front. Finding and remediation plan

**Status (2026-10-02, end of session): Phase 0 decided. Phase 1 script built, tested, canary run. Phase 2 built locally, NOT deployed.** Everything in sections 1 and 2 was measured read-only against live Firestore (`cfm-qbo-mcp`), live Front, and the `front-ai-review` Cloud Run logs.

**Phase 0 decisions (Jay, 2026-10-02): yes to all three.** (1) Archive the FYI auto-expired loops too. (2) Archive reply-closed loops too. (3) Where the sender wrote after the loop was closed, leave the email open and list it.

**Added by Jay the same day: "anything prior to 5/1 should be archived."** Defined with Jay as "newest message before 5/1" across CFM, EDOM and DME Finance. **Done 10/2 to 10/3:** 11,030 conversations archived (`archive_before_cutoff.py`), 76 left open on purpose, verified against live Front. See the 26-119 `CLAUDE.md` checkpoint.

**Phase 1 result (10/2):** 1,639 closed-loop emails archived, 998 stamp-only, 68 held (list: `HELD - closed loops where the sender wrote after (2026-10-02).csv`), 0 errors, verified against live Front. **Still open: deploying Phase 2 (the pipeline sweep), pending Jay's go.**

**Progress**
- Phase 1: `archive_closed_loops_backlog.py` built (dry-run by default, newer-reply guard, per-loop CSV with prior state, `--rollback`). 29 tests pass. Live dry run on 25 loops, then a **10-loop canary: 10 of 10 archived and each re-verified in Front** (report in `data/backlog/`). The full pass is still to run.
- Phase 2 (built locally, tests pass, **not deployed**): `cos/archive_sweep.py` plus a hook in `pipeline.py`; `resolve_loop` now marks every closed Front loop `front_archived=False` with `resolved_at`; `cos/front_archive.py` retries 429; the triage importer counts unknown actions as errors and counts archive failures; the upload endpoint keeps the file in the bucket when rows errored (the workbook already claimed it did). Full suite 189 pass; the 2 failures are the known pre-existing `test_sender.py` ones.
- Not done: the VBA "Upload Complete" message does not yet show the new counts (needs a workbook rebuild and redistribution); unknown actions already surface through the existing "N row(s) had errors" line.

## 1. Short answer

**Yes, closed loops are being left open in Front. But the triage upload itself does archive.** When an action is recognised, the upload resolves the loop and archives the Front conversation. The Cloud Run logs show this working on every upload from 9/4 through 10/2 ("archived in Front" / "already 'archived' in Front"; zero archive warnings).

The email stays open for four other reasons:

| # | Cause | Effect | Status |
|---|-------|--------|--------|
| A | **The dropdown bug.** The workbook offered `"done` (stray quote) as its first option, and the server read it as an unknown action and skipped it. Seen in the logs on 15 of the 18 upload days, 9/4 to 9/25 (130 distinct loops). The "Upload Complete" box counted FYI marks as "Done" and never mentioned skipped rows, so it looked like it worked. | The loop was **not closed at all** and nothing was archived. Today 112 of the 130 are done, 10 are dropped, and **8 are still open** (#4046, 4352, 4890, 4977, 5081, 5530, 5670, 5814). | Fixed 10/2 (`8e5adff`, live on revision `front-ai-review-00056-2bg`). Silent-skip reporting is **not** fixed (Phase 2). |
| B | **Loops closed outside the triage upload are never archived.** These are the reply-detected closure in `cos/extract.py::reconcile` (deliberately not archived, because "a reply leaves the thread open"), the 24-hour FYI auto-expiry `expire_fyi_loops` (1,332 loops, deliberately not archived), and the `cos_resolve_loop` MCP tool Claude sessions use. | The loop leaves the list; the email stays in the inbox. This is the bulk of the backlog. | Design gap, still open. |
| C | **The archive step is one-shot and best-effort.** `front_archive.archive_conversation` returns False on any Front error. On a 429 the client sleeps and then raises, and nobody retries. The loop is already resolved by then, and nothing ever revisits it. | A rate-limited or flaky moment leaves a closed loop open in Front permanently. | Design gap, still open. |
| D | **The measuring stick is wrong.** `front_archived=True` is a stamp, not Front's real status. And `poll_front_archived.py` and `backfill_archive_front_resolved.py` build their own Firestore client from `GCP_PROJECT=cfm-front-mail`, the old database from before the 6/7 migration to `cfm-qbo-mcp`. The "1,511 of 1,511 stamped" note in CLAUDE.md is about a stale database. | Nobody has been able to see this gap. **Running the old backfill as-is would touch the wrong database.** | Fix in Phase 1. |

## 2. How big is it (live numbers, 2026-10-02)

- Resolved Front loops (done or dropped): **5,937**. Stamped as archived: 3,248. **Unstamped: 2,689** (1,354 done, 1,332 FYI auto-expired, 3 other).
- The stamp is not the truth. A **full read-only scan of all 2,689 against live Front** (10/2) found:

| Front status | Done | FYI auto-expired | Dropped | Total |
|---|---|---|---|---|
| **Still open (assigned or unassigned)** | 676 | 1,013 | 3 | **1,692** |
| Already archived or deleted (stamp missing only) | 678 | 319 | 0 | 997 |

  Of the 1,692 open: **1,624 are archive candidates** (616 done, 1,005 FYI, 3 dropped) and **68 are HELD** because the sender wrote after the loop was closed (60 done, 8 FYI). 633 are assigned to jay@cfmins.org, 1,030 are unassigned, 26 are assigned to admin@cfmins.org, and 3 to other teammates.
- 31 of 32 sampled loops from the 7/27 bulk import were archived in Front; only the stamp was missing, so that batch is mostly harmless.
- **A trap in the backlog (now guarded):** about 4% of the open conversations got a **newer message after the loop was closed** (for example #5369 closed 9/18, sender wrote again 9/22). Archiving blindly would bury a fresh reply. These are held and listed instead.

## 3. Remediation plan

### Phase 0: decisions (Jay)
1. **Archive the 1,332 FYI auto-expired loops too?** Recommended: yes. They are closed loops and Front reopens an archived conversation when the sender writes again. This is a separate switch, so you can say no.
2. **Archive reply-closed loops too?** Recommended: yes, same reasoning.
3. **Newer-reply policy.** For a closed loop whose conversation got a newer unreplied inbound message: leave it open in Front and list it for you (recommended), or archive regardless.

### Phase 1: backlog cleanup (new script, dry run first)
New `archive_closed_loops_backlog.py`. The old backfill is left untouched except for a one-line project fix.
- **Reads through `cos.ledger`** (project `cfm-qbo-mcp`) so it cannot use the wrong database again. Selects channel=front, status done or dropped, `front_archived` not True, filtered by the Phase 0 scope.
- **Dry-run by default.** One GET per conversation. Writes a CSV report with loop #, counterparty, summary, Front status, assignee, `waiting_since`, and the planned action: `archive`, `stamp-only` (already archived, deleted, or 404), or `HOLD-newer-inbound`.
- **Guard:** `waiting_since` later than the loop's resolution time means HOLD (not archived, listed for review).
- **`--execute`:** archives, then stamps `front_archived=True` immediately per loop, so it is resumable and idempotent.
- **429 handling:** the Front client already sleeps `Retry-After` and then raises, so the script retries the same item up to 4 times. Pace roughly 40 requests per minute, well under the limit, so the live pipeline is not starved.
- **Rollout:** canary of 10, spot-check each in Front, then batches of about 250, then a final full verification scan. Target: zero closed-loop conversations open in Front, apart from the HOLD list.
- **Rollback:** the CSV records each conversation's prior status and assignee. Archiving is reversible, so a bulk reopen is possible from it.
- **Time:** about 30 to 45 minutes for the scan plus archive pass, run on this PC.

### Phase 2: permanent fix (so this stops recurring)
1. **Self-healing sweep in the pipeline.** After reconcile and FYI expiry, each run archives any resolved Front loop that is not stamped, capped at about 100 per run, with the 429 retry and the same newer-inbound guard. One change covers causes B and C together: reply closures, FYI expiry, the MCP tool, and rate-limit misses. Lives in `cos/front_archive.py`; deploy by push to `main` (CI).
2. **`archive_conversation` retries 429** internally instead of returning False after one failure.
3. **Triage upload reports the truth.** The response and the "Upload Complete" box get separate counts for archived, archive failed, unknown action skipped, and FYI marked, instead of one inflated "Done" number.
4. **Fix the diagnostics:** `poll_front_archived.py` and `backfill_archive_front_resolved.py` use `FIRESTORE_PROJECT`. Add a "closed but open in Front" check against Front's real status.
5. **Optional:** reopen a loop when the sender writes after we closed it. Today a closed loop is never reopened by a later reply. This is a separate feature and a separate decision.

### Phase 3: verification and docs
- Re-run the full read-only scan after Phase 1 (expect zero except HOLD).
- Watch the next 3 daily pipeline runs for sweep counts and any archive failures.
- Update the 26-119 `CLAUDE.md` (the "1,511 of 1,511" claim is stale), `TODOS.md`, and the tracker row.

## 4. Evidence
- Cloud Run `front-ai-review` logs 9/2 to 10/2: 18 upload days. 10/2 uploads show "archived in Front" lines and no unknown-action lines.
- Firestore `loops` and `feedback`, whole collection: 6,098 loops.
- Front sample: 140 conversations, checked live.
- Code read: `cos_triage_import.py`, `cos/front_archive.py`, `cos/extract.py`, `cos_mcp_server.py`, `mcp_server.py`, `front_client.py`, `modTriage.bas`.

## 5. Not verified
- Why the 7/27 batch is unstamped although it is archived in Front.
- The exact backlog numbers, until the full scan completes.
- Logs before 9/2 are past the 30-day retention.

## 6. Scratch artifacts (session scratchpad, not in the repo)
`diag_unarchived.py`, `diag_front_sample.py`, `diag_skipped_done.py`, `diag_full_scan_v2.py`, `full_scan_v2.json`.
