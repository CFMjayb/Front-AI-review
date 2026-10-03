"""Archive, in Front, every open conversation whose NEWEST message is before a cutoff.

Jay (2026-10-02): "anything prior to 5/1 should be archived", across the CFM, EDOM
and DME Finance inboxes. "Prior to 5/1" means the conversation has had no message
since then. NOT the creation date: Front's search `before:` filter matches creation,
and 23 of 25 sampled `before:5/1` hits had real activity after 5/1 (some that day).
See reference memory front-search-before-after-is-creation-date.

Two steps, so nothing is archived on a guess:

  1. PLAN (read-only, the default). Enumerate open conversations created before the
     cutoff (the only ones that can qualify), skip the ones Front's own fields prove
     active (waiting_since after the cutoff), then read the messages of the rest to
     get the real newest-message date. Writes a plan CSV with a verdict per
     conversation: archive | active | draft | other-assignee | unknown.
  2. EXECUTE (--execute --plan <csv>). For each `archive` row, re-read the
     conversation just before acting (still open? changed since the plan? if it
     changed, re-check its messages), PATCH it archived, log prior status + assignee
     for --rollback.

Never archived, only listed: anything with an unreplied inbound after the cutoff,
any conversation holding a draft (someone is working on it), anything assigned to a
teammate other than the allowed set (default: unassigned or jay@cfmins.org), and
anything whose messages could not be read.

Usage:
    python archive_before_cutoff.py --enumerate-only                  # counts, ~2 min
    python archive_before_cutoff.py                                   # full plan (read-only)
    python archive_before_cutoff.py --execute --plan data/before_cutoff/plan_X.csv --limit 10 --verify 10   # canary
    python archive_before_cutoff.py --execute --plan data/before_cutoff/plan_X.csv                        # the rest
    python archive_before_cutoff.py --rollback data/before_cutoff/exec_X.csv --execute                    # reopen
"""
import argparse
import csv
import datetime
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

os.environ.setdefault("GCP_PROJECT", "cfm-front-mail")
os.environ.setdefault("USE_SECRET_MANAGER", "true")

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from cos import front_archive
from cos.front_archive import Throttle, call_with_retry, OPEN_STATUSES

# Midnight Eastern on 5/1/2026 (EDT, UTC-4).
DEFAULT_CUTOFF = "2026-05-01"
INBOXES = {"inb_csx96": "CFM (Jay)", "inb_cv4ii": "EDOM (Jay)", "inb_cr72y": "DME Finance (shared)"}
DEFAULT_ALLOWED = (None, "jay@cfmins.org")
MAX_MESSAGES = 300            # a thread this long is treated as unknown rather than guessed at
MAX_ERRORS = 40

ARCHIVE = "archive"
ACTIVE = "active"
DRAFT = "draft"
OTHER_ASSIGNEE = "other-assignee"
UNKNOWN = "unknown"
NEED_MESSAGES = "need-messages"

PLAN_FIELDS = ["conv_id", "inbox", "created", "updated_at", "waiting_since", "assignee", "subject",
               "newest_msg", "has_draft", "verdict", "reason"]
EXEC_FIELDS = ["ts_utc", "conv_id", "inbox", "subject", "prior_status", "assignee", "result"]


def cutoff_epoch(day: str) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime.datetime(y, m, d, 4, 0, 0, tzinfo=datetime.timezone.utc).timestamp())


def _iso(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _fnum(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def precheck(c: dict, cutoff: float, allowed=DEFAULT_ALLOWED) -> tuple[str, str]:
    """Decide from Front's own list fields, before spending a messages call.

    c carries waiting_since / updated_at (epoch seconds) and assignee (email or None).
    updated_at is always >= the newest message, and waiting_since is always <= it, so:
      waiting_since >= cutoff  -> a message after the cutoff exists  -> ACTIVE
      updated_at   <  cutoff   -> nothing at all after the cutoff    -> ARCHIVE
    """
    ws, upd = _fnum(c.get("waiting_since")), _fnum(c.get("updated_at"))
    if ws is not None and ws >= cutoff:
        return ACTIVE, "unreplied inbound after the cutoff"
    if (c.get("assignee") or None) not in allowed:
        return OTHER_ASSIGNEE, f"assigned to {c.get('assignee')}"
    if upd is not None and upd < cutoff:
        return ARCHIVE, "no activity of any kind since before the cutoff"
    return NEED_MESSAGES, "must read the messages"


def decide_from_messages(messages: Optional[list], cutoff: float) -> tuple[str, str, Optional[float], bool]:
    """Verdict from the real messages: (verdict, reason, newest_non_draft_ts, has_draft)."""
    if messages is None:
        return UNKNOWN, "could not read messages", None, False
    if len(messages) >= MAX_MESSAGES:
        return UNKNOWN, f"{len(messages)}+ messages, not read in full", None, False
    has_draft = any(m.get("is_draft") for m in messages)
    stamps = [float(m["created_at"]) for m in messages
              if not m.get("is_draft") and m.get("created_at")]
    newest = max(stamps) if stamps else None
    if has_draft:
        return DRAFT, "has a draft (someone is working on it)", newest, True
    if newest is None:
        return UNKNOWN, "no readable message timestamps", None, False
    if newest >= cutoff:
        return ACTIVE, "newest message is after the cutoff", newest, False
    return ARCHIVE, "newest message is before the cutoff", newest, False


def enumerate_candidates(front: Any, inbox: str, cutoff: float, throttle: Throttle,
                         limit: int = 0, say=print) -> tuple[list[dict], Optional[int]]:
    """Open conversations CREATED before the cutoff in one inbox, plus Front's _total."""
    from front_client import _request, _result_items, _next_page
    q = f"is:open inbox:{inbox} before:{int(cutoff)}"
    nxt: Optional[str] = f"/conversations/search/{quote(q, safe='')}"
    first, total, out = True, None, []
    while nxt:
        throttle.wait()
        payload, _ = call_with_retry(_request, "GET", nxt, front.token,
                                         params={"limit": 100} if first else None)
        if first:
            total = (payload or {}).get("_total")
            first = False
        for i in _result_items(payload):
            out.append({"conv_id": i["id"], "inbox": inbox, "created_at": i.get("created_at"),
                        "updated_at": i.get("updated_at"), "waiting_since": i.get("waiting_since"),
                        "assignee": (i.get("assignee") or {}).get("email"),
                        "subject": (i.get("subject") or "")[:70].replace("\n", " ")})
        if len(out) % 1000 < 100:
            say(f"    {INBOXES.get(inbox, inbox)}: {len(out)} listed...")
        if limit and len(out) >= limit:
            out = out[:limit]
            break
        nxt = _next_page(payload)
    return out, total


def read_messages(front: Any, conv_id: str, throttle: Throttle) -> Optional[list]:
    try:
        throttle.wait()
        return call_with_retry(front.get_conversation_messages, conv_id, max_pages=6)
    except Exception:
        return None


def _row(c: dict, verdict: str, reason: str, newest=None, has_draft=False) -> dict:
    return {"conv_id": c["conv_id"], "inbox": c["inbox"], "created": _iso(c.get("created_at")),
            "updated_at": _iso(c.get("updated_at")), "waiting_since": _iso(c.get("waiting_since")),
            "assignee": c.get("assignee") or "", "subject": c.get("subject", ""),
            "newest_msg": _iso(newest), "has_draft": "yes" if has_draft else "",
            "verdict": verdict, "reason": reason}


def build_plan(front: Any, args, say=print) -> int:
    cutoff = cutoff_epoch(args.cutoff)
    throttle = Throttle(args.interval)
    inboxes = [i for i in (args.inbox or list(INBOXES)) if i]
    allowed = tuple(DEFAULT_ALLOWED) + tuple(args.also_assignee or ())
    say(f"Cutoff {args.cutoff} 00:00 ET ({_iso(cutoff)})   inboxes: {', '.join(INBOXES.get(i, i) for i in inboxes)}")

    cands: list[dict] = []
    for inbox in inboxes:
        items, total = enumerate_candidates(front, inbox, cutoff, throttle, limit=args.limit, say=say)
        flag = "" if total in (None, len(items)) or args.limit else f"  (Front says _total={total}!)"
        say(f"  {INBOXES.get(inbox, inbox)}: {len(items)} open conversations created before the cutoff{flag}")
        cands.extend(items)

    pre = {}
    for c in cands:
        v, _ = precheck(c, cutoff, allowed)
        pre[v] = pre.get(v, 0) + 1
    say(f"Precheck (no messages read yet): {pre}")
    if args.enumerate_only:
        return 0

    out_dir = Path(__file__).parent / "data" / "before_cutoff"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Path(args.report) if args.report else out_dir / f"plan_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv"
    done_ids: set[str] = set()
    mode = "w"
    if report.exists():                                   # resume
        with open(report, newline="", encoding="utf-8") as fh:
            done_ids = {r["conv_id"] for r in csv.DictReader(fh)}
        mode = "a"
        say(f"Resuming {report.name}: {len(done_ids)} already classified")

    tally: dict[str, int] = {}
    with open(report, mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_FIELDS)
        if mode == "w":
            w.writeheader()
        for n, c in enumerate(cands, 1):
            if c["conv_id"] in done_ids:
                continue
            verdict, reason = precheck(c, cutoff, allowed)
            newest, has_draft = None, False
            if verdict == NEED_MESSAGES:
                verdict, reason, newest, has_draft = decide_from_messages(
                    read_messages(front, c["conv_id"], throttle), cutoff)
            w.writerow(_row(c, verdict, reason, newest, has_draft))
            fh.flush()
            tally[verdict] = tally.get(verdict, 0) + 1
            if n % 200 == 0:
                say(f"  ...{n}/{len(cands)}  {tally}")
    say(f"\nPlan written: {report}\nThis run: {tally}")
    say("Read-only: nothing was archived. Review the counts, then run with --execute --plan <that csv>.")
    return 0


def execute_plan(front: Any, args, say=print) -> int:
    cutoff = cutoff_epoch(args.cutoff)
    allowed = tuple(DEFAULT_ALLOWED) + tuple(args.also_assignee or ())
    with open(args.plan, newline="", encoding="utf-8") as fh:
        plan = [r for r in csv.DictReader(fh) if r["verdict"] == ARCHIVE]
    if args.limit:
        plan = plan[:args.limit]
    say(f"{len(plan)} conversations planned for archive (cutoff {args.cutoff})")
    throttle = Throttle(args.interval)
    out_dir = Path(args.plan).parent
    report = Path(args.report) if args.report else out_dir / f"exec_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv"
    tally: dict[str, int] = {}
    errors = 0
    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EXEC_FIELDS)
        w.writeheader()
        for n, p in enumerate(plan, 1):
            row = {"ts_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "conv_id": p["conv_id"], "inbox": p["inbox"], "subject": p["subject"],
                   "prior_status": "", "assignee": "", "result": ""}
            row["result"] = _execute_one(front, p, row, cutoff, allowed, throttle,
                                         verify=bool(args.verify and n <= args.verify))
            w.writerow(row)
            fh.flush()
            key = row["result"].split(":")[0]
            tally[key] = tally.get(key, 0) + 1
            if key == "error":
                errors += 1
                say(f"  {p['conv_id']} {row['result']}")
                if errors >= MAX_ERRORS:
                    say(f"Stopping: {MAX_ERRORS} errors. Re-run with the same plan to resume (finished ones are skipped as already archived).")
                    break
            if n % 100 == 0:
                say(f"  ...{n}/{len(plan)}  {tally}")
    say(f"\nDone. {tally}\nReport (use it for --rollback): {report}")
    return 1 if errors >= MAX_ERRORS else 0


def _execute_one(front, p: dict, row: dict, cutoff: float, allowed, throttle: Throttle, *, verify: bool) -> str:
    cid = p["conv_id"]
    try:
        throttle.wait()
        conv = call_with_retry(front.get_conversation, cid)
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return "gone"
        return f"error: could not read: {exc}"
    row["prior_status"] = conv.get("status") or ""
    row["assignee"] = (conv.get("assignee") or {}).get("email") or ""
    if row["prior_status"] not in OPEN_STATUSES:
        return "already-archived"
    # Re-check anything that could have changed since the plan was made.
    verdict, _ = precheck({"waiting_since": conv.get("waiting_since"), "updated_at": conv.get("updated_at"),
                           "assignee": row["assignee"] or None}, cutoff, allowed)
    if verdict == NEED_MESSAGES:
        verdict, _, _, _ = decide_from_messages(read_messages(front, cid, throttle), cutoff)
    if verdict != ARCHIVE:
        return f"skipped: changed since the plan ({verdict})"
    try:
        throttle.wait()
        call_with_retry(front.set_status, cid, "archived")
        if verify:
            throttle.wait()
            if (call_with_retry(front.get_conversation, cid).get("status") or "") in OPEN_STATUSES:
                return "error: PATCH returned OK but the conversation is still open"
    except Exception as exc:
        return f"error: {exc}"
    return "archived"


def rollback(csv_path: str, *, execute: bool, front: Any) -> int:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("result") == "archived"]
    print(f"{len(rows)} archived rows in {csv_path}   mode: {'EXECUTE' if execute else 'DRY RUN'}")
    if not execute:
        print("Dry run only. Re-run with --execute to reopen them.")
        return 0
    throttle, ok, bad = Throttle(), 0, 0
    for r in rows:
        try:
            throttle.wait()
            call_with_retry(front.set_status, r["conv_id"], "open")
            ok += 1
        except Exception as exc:
            bad += 1
            print(f"  {r['conv_id']} could not reopen: {exc}")
    print(f"Reopened {ok}, failed {bad}")
    return 0 if not bad else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="YYYY-MM-DD (midnight Eastern); default 2026-05-01")
    p.add_argument("--inbox", action="append", help="inbox id; repeat. Default: CFM, EDOM, DME Finance")
    p.add_argument("--also-assignee", action="append", help="also archive conversations assigned to this teammate email")
    p.add_argument("--enumerate-only", action="store_true", help="just count candidates by precheck")
    p.add_argument("--execute", action="store_true", help="archive (requires --plan); default is the read-only plan")
    p.add_argument("--plan", default="", help="plan CSV to execute")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between Front requests (default 1.0)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--verify", type=int, default=0, help="re-read the first N archived to confirm")
    p.add_argument("--report", default="", help="output CSV path (plan or exec; an existing plan path resumes)")
    p.add_argument("--rollback", default="", metavar="CSV", help="reopen what that exec CSV archived")
    args = p.parse_args(argv)

    from front_client import FrontClient
    from auth import get_front_api_token
    front = FrontClient(get_front_api_token())
    if args.rollback:
        return rollback(args.rollback, execute=args.execute, front=front)
    if args.execute:
        if not args.plan:
            print("--execute needs --plan <csv from a plan run>")
            return 2
        return execute_plan(front, args)
    return build_plan(front, args)


if __name__ == "__main__":
    sys.exit(main())
