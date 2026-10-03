"""Archive, in Front, every conversation whose loop is already closed.

Why this exists (2026-10-02, see "Remediation Plan - Archive Closed Loops"):
closed loops were being left open in Front (reply-closed loops, 24h FYI expiry,
the cos_resolve_loop tool, rate-limited archive attempts). The old backfill and
poll scripts also read the wrong Firestore project, so nobody could see it.

What it does, per closed Front loop that is not yet stamped front_archived:
  1. GET the conversation (the only truth is Front's real status).
  2. Classify (cos/front_archive.classify_for_archive):
       ARCHIVE  still open, nothing new since we closed it  -> PATCH archived, stamp
       STAMP    Front already archived/deleted/spam/gone     -> stamp only
       HOLD     open, but the sender wrote AFTER we closed it -> leave alone, list it
  3. Log one CSV row per loop (with the prior Front status + assignee, so any
     archive can be reversed with --rollback).

SAFE BY DEFAULT: with no flags this is a read-only DRY RUN (GETs only).
Nothing is written to Front or Firestore without --execute.

Usage:
    python archive_closed_loops_backlog.py                         # dry run, all
    python archive_closed_loops_backlog.py --limit 10 --execute --verify 10   # canary
    python archive_closed_loops_backlog.py --execute               # everything left
    python archive_closed_loops_backlog.py --scope done|fyi|all    # subset
    python archive_closed_loops_backlog.py --rollback data/backlog/<file>.csv --execute

Re-runnable: each loop is stamped front_archived=True the moment it is handled,
so a re-run (or a crash/resume) only sees what is left.
"""
import argparse
import csv
import datetime
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

os.environ.setdefault("LEDGER_BACKEND", "firestore")
os.environ.setdefault("GCP_PROJECT", "cfm-front-mail")
os.environ.setdefault("USE_SECRET_MANAGER", "true")

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

LEDGER_PROJECT = "cfm-qbo-mcp"        # the CoS ledger lives here, NOT in GCP_PROJECT
os.environ["FIRESTORE_PROJECT"] = os.environ.get("FIRESTORE_PROJECT") or LEDGER_PROJECT

from cos import front_archive
from cos.front_archive import ARCHIVE, STAMP, HOLD

FYI_REASON = "fyi auto-expire 24h"
MIN_INTERVAL_S = 1.0                   # ~60 Front requests/min — the rate the read-only scan proved clean
MAX_ERRORS = 25
CSV_FIELDS = ["ts_utc", "mode", "loop_id", "num", "counterparty", "summary", "group",
              "source_ref", "front_status", "assignee", "waiting_since", "resolved_ts",
              "verdict", "result"]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_iso(v) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(v), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


class Throttle:
    """Keep consecutive Front requests at least `interval` seconds apart."""
    def __init__(self, interval: float = MIN_INTERVAL_S):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        gap = self.interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


def group_of(loop: dict, reason: Optional[str]) -> str:
    if reason == FYI_REASON:
        return "fyi"
    return loop.get("status") or "?"


def select_loops(db, scope: str = "all") -> list[dict]:
    """Closed Front loops not yet stamped, each carrying `_resolved_ts` and `_group`.

    Reads Firestore directly: cos.ledger.list_loops() silently drops deferred
    loops, and last_reviewed is bumped by every patch so it is not a resolution
    time — the feedback log's ts is.
    """
    resolved_ts: dict[str, str] = {}
    reason_by: dict[str, Optional[str]] = {}
    for s in db.collection("feedback").stream():
        d = s.to_dict()
        if d.get("action") not in ("done", "dropped"):
            continue
        lid = d.get("loop_id")
        if lid and (lid not in resolved_ts or (d.get("ts") or "") > resolved_ts[lid]):
            resolved_ts[lid] = d.get("ts") or ""
            reason_by[lid] = d.get("reason")

    out = []
    for s in db.collection("loops").stream():
        d = s.to_dict()
        if not (d.get("channel") == "front" and d.get("status") in ("done", "dropped")
                and d.get("source_ref") and d.get("front_archived") is not True):
            continue
        d["id"] = s.id
        d["_resolved_ts"] = resolved_ts.get(s.id) or ""
        d["_group"] = group_of(d, reason_by.get(s.id))
        if scope == "done" and d["_group"] != "done":
            continue
        if scope == "fyi" and d["_group"] != "fyi":
            continue
        out.append(d)
    out.sort(key=lambda l: l.get("num") or 0)
    return out


def process_loop(front: Any, loop: dict, *, execute: bool, verify: bool,
                 stamp: Callable[[str], None], throttle: Optional[Throttle] = None) -> dict:
    """Classify one closed loop and (if execute) act on it. Returns a CSV row dict.

    result is one of: dry-run | archived | stamped | held | error: <why>
    """
    src = loop["source_ref"]
    resolved_ts = loop.get("_resolved_ts") or ""
    row = {"ts_utc": _now(), "mode": "execute" if execute else "dry-run",
           "loop_id": loop["id"], "num": loop.get("num"),
           "counterparty": (loop.get("counterparty") or "")[:40],
           "summary": (loop.get("summary") or "")[:70].replace("\n", " "),
           "group": loop.get("_group"), "source_ref": src, "front_status": "",
           "assignee": "", "waiting_since": "", "resolved_ts": resolved_ts,
           "verdict": "", "result": ""}

    def call(fn, *a):
        if throttle:
            throttle.wait()
        return front_archive.call_with_429_retry(fn, *a)

    try:
        conv = call(front.get_conversation, src)
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            conv = None
            row["front_status"] = "404"
        else:
            row["result"] = f"error: could not read Front: {exc}"
            return row

    if conv is not None:
        row["front_status"] = conv.get("status") or ""
        row["assignee"] = (conv.get("assignee") or {}).get("email") or ""
        row["waiting_since"] = _epoch_iso(conv.get("waiting_since"))

    verdict = front_archive.classify_for_archive(conv, resolved_ts)
    # No recorded resolution time: we cannot prove nothing new arrived, so an
    # open conversation is held for a human rather than archived on a guess.
    if verdict == ARCHIVE and not resolved_ts:
        verdict = HOLD
    row["verdict"] = verdict

    if verdict == HOLD:
        row["result"] = "held"
        return row
    if not execute:
        row["result"] = "dry-run"
        return row

    try:
        if verdict == ARCHIVE:
            call(front.set_status, src, "archived")
            if verify:
                after = call(front.get_conversation, src)
                if (after.get("status") or "") in front_archive.OPEN_STATUSES:
                    row["result"] = "error: PATCH returned OK but conversation is still open"
                    return row
            stamp(loop["id"])
            row["result"] = "archived"
        else:
            stamp(loop["id"])
            row["result"] = "stamped"
    except Exception as exc:
        row["result"] = f"error: {exc}"
    return row


def run(args, front=None, db=None) -> int:
    if front is None:
        from front_client import FrontClient
        from auth import get_front_api_token
        front = FrontClient(get_front_api_token())
    if db is None:
        from cos import ledger_firestore as lf
        db = lf._db()
    project = getattr(db, "project", None)
    if project and project != LEDGER_PROJECT:
        print(f"ABORT: Firestore client points at {project!r}, the ledger is {LEDGER_PROJECT!r}.")
        return 2
    print(f"Ledger project: {project or LEDGER_PROJECT}   mode: "
          f"{'EXECUTE' if args.execute else 'DRY RUN (read-only)'}   scope: {args.scope}")

    loops = select_loops(db, args.scope)
    if args.limit:
        loops = loops[:args.limit]
    print(f"{len(loops)} closed Front loops to process")
    if not loops:
        return 0

    out_dir = Path(__file__).parent / "data" / "backlog"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Path(args.report) if args.report else out_dir / (
        f"backlog_{'execute' if args.execute else 'dryrun'}_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")

    def stamp(loop_id: str) -> None:
        # Direct update, NOT patch_loop: patch_loop bumps last_reviewed.
        db.collection("loops").document(loop_id).update({"front_archived": True})

    throttle = Throttle()
    tally: dict[str, int] = {}
    errors = 0
    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for i, loop in enumerate(loops, 1):
            row = process_loop(front, loop, execute=args.execute,
                               verify=bool(args.verify and i <= args.verify),
                               stamp=stamp, throttle=throttle)
            w.writerow(row)
            fh.flush()
            key = row["result"].split(":")[0]
            tally[key] = tally.get(key, 0) + 1
            if key == "error":
                errors += 1
                print(f"  #{row['num']} {row['result']}")
                if errors >= MAX_ERRORS:
                    print(f"Stopping: {MAX_ERRORS} errors. Re-run to resume.")
                    break
            if i % 50 == 0:
                print(f"  ...{i}/{len(loops)}  {tally}", flush=True)

    held = tally.get("held", 0)
    print(f"\nDone. {tally}")
    print(f"Report: {report}")
    if held:
        print(f"{held} HELD (sender wrote after the loop was closed) — still open in Front, see the 'held' rows.")
    if not args.execute:
        print("Dry run only. Re-run with --execute to apply.")
    return 1 if errors >= MAX_ERRORS else 0


def rollback(csv_path: str, *, execute: bool, front=None, db=None) -> int:
    """Re-open every conversation this script archived (rows with result=archived)."""
    if front is None:
        from front_client import FrontClient
        from auth import get_front_api_token
        front = FrontClient(get_front_api_token())
    if db is None:
        from cos import ledger_firestore as lf
        db = lf._db()
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("result") == "archived"]
    print(f"{len(rows)} archived rows in {csv_path}   mode: {'EXECUTE' if execute else 'DRY RUN'}")
    if not execute:
        print("Dry run only. Re-run with --execute to reopen them.")
        return 0
    throttle = Throttle()
    reopened = failed = 0
    for r in rows:
        try:
            throttle.wait()
            front_archive.call_with_429_retry(front.set_status, r["source_ref"], "open")
            db.collection("loops").document(r["loop_id"]).update({"front_archived": False})
            reopened += 1
        except Exception as exc:
            failed += 1
            print(f"  #{r.get('num')} could not reopen: {exc}")
    print(f"Reopened {reopened}, failed {failed}")
    return 0 if not failed else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--execute", action="store_true", help="actually write (default is a read-only dry run)")
    p.add_argument("--scope", choices=["all", "done", "fyi"], default="all")
    p.add_argument("--limit", type=int, default=0, help="only the first N loops (by loop #)")
    p.add_argument("--verify", type=int, default=0, help="re-read the first N archived conversations to confirm")
    p.add_argument("--report", default="", help="CSV path (default data/backlog/...)")
    p.add_argument("--rollback", default="", metavar="CSV", help="reopen what that report archived")
    args = p.parse_args(argv)
    if args.rollback:
        return rollback(args.rollback, execute=args.execute)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
