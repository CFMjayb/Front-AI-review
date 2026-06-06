"""Import a reviewed CoS triage spreadsheet and apply actions to Firestore.

Usage:
    python cos_triage_import.py [path_to_xlsx]

If path_to_xlsx is omitted, picks the most recent file in data/triage/.

Action column values (case-insensitive):
    done                  — resolve loop as done
    drop                  — resolve loop as dropped
    exclude               — drop + tag as junk (helps CoS avoid re-classifying similar emails)
    subscribe             — tag in Front as "cos/reading-list" + drop; view latest in Front
    fyi                   — re-classify as FYI notification (grey, auto-clears 24h)
    defer                 — move to Deferred section; hidden from main list and briefing
    snooze YYYY-MM-DD     — snooze until that date
    snooze 1d / 2d / 3d   — snooze for N days
    snooze 1w / 2w        — snooze for N weeks
    snooze 1m             — snooze for 1 month
    (blank)               — no action change; Notes column still saved if filled in
"""
import datetime
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("LEDGER_BACKEND", os.environ.get("LEDGER_BACKEND", "firestore"))
os.environ.setdefault("GCP_PROJECT", os.environ.get("GCP_PROJECT", "cfm-front-mail"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from cos import ledger
import openpyxl

# ── Lazy Front client (created on first archive call) ────────────────────────
_front_client: Optional[Any] = None


def _get_front() -> Any:
    global _front_client
    if _front_client is None:
        from front_client import FrontClient
        from auth import get_front_api_token
        _front_client = FrontClient(get_front_api_token())
    return _front_client


_FRONT_OPEN_STATUSES = {"open", "assigned", "unassigned"}


def _archive_in_front(loop_rec: Optional[dict], num: Any) -> bool:
    """Archive the source Front conversation when a loop is resolved.

    Only acts on loops with channel=front and a source_ref.  Warns but does not
    raise on failure so a Front error never blocks a Firestore update.

    Checks the current Front status first:
      - Already non-open (archived/spam/deleted) → no PATCH needed, return True
        so the caller stamps front_archived=True in Firestore.
      - 404 → gone from Front → same, return True.
      - Still open → PATCH to archived → return True.
      - Any other error → warn and return False.

    Returns True if Firestore should be stamped front_archived=True.
    """
    if not loop_rec or loop_rec.get("channel") != "front":
        return False
    src = loop_rec.get("source_ref")
    if not src:
        return False
    front = _get_front()
    try:
        conv = front.get_conversation(src)
        front_status = conv.get("status") or ""
    except Exception as exc:
        # 404 → conversation gone; treat as already resolved.
        # Other errors → warn, don't stamp (status unknown).
        status_code = getattr(exc, "status", None)
        if status_code == 404:
            print(f"    → Front conversation not found ({src}) — stamping anyway")
            return True
        print(f"    WARNING: could not fetch Front status for {src}: {exc}")
        return False

    if front_status not in _FRONT_OPEN_STATUSES:
        # Already archived / spam — Front agrees, nothing to change.
        print(f"    → already {front_status!r} in Front ({src}) — stamping")
        return True

    # Conversation is still open — archive it.
    try:
        front.set_status(src, "archived")
        print(f"    → archived in Front ({src})")
        return True
    except Exception as exc:
        print(f"    WARNING: could not archive {src} in Front: {exc}")
        return False


def _parse_snooze_until(value: str) -> str | None:
    """Parse snooze target. Returns ISO datetime string or None on failure."""
    v = value.strip().lower()
    now = datetime.datetime.now(datetime.timezone.utc)

    # snooze YYYY-MM-DD
    m = re.match(r"snooze\s+(\d{4}-\d{2}-\d{2})$", v)
    if m:
        return m.group(1) + "T00:00:00Z"

    # snooze Nd (days) or Nw (weeks) or Nm (months)
    m = re.match(r"snooze\s+(\d+)([dwm])$", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            delta = datetime.timedelta(days=n)
        elif unit == "w":
            delta = datetime.timedelta(weeks=n)
        else:  # months — approximate as 30 days each
            delta = datetime.timedelta(days=n * 30)
        until = now + delta
        return until.strftime("%Y-%m-%dT%H:%M:%SZ")

    return None


def _find_latest_export() -> Path | None:
    triage_dir = Path(__file__).parent / "data" / "triage"
    if not triage_dir.exists():
        return None
    # Match date-stamped files: "CoS Triage YYYY-MM-DD.xlsx" (old) or
    # "CoS Triage YYYY-MM-DD HH-MM.xlsx" (new).  Excludes test/scratch files.
    dated = sorted(
        triage_dir.glob("CoS Triage [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*.xlsx"),
        reverse=True
    )
    return dated[0] if dated else None


def _col_index(headers: list[str], name: str) -> int | None:
    try:
        return headers.index(name)
    except ValueError:
        return None


def _run_triage_sheet(wb: openpyxl.Workbook) -> dict:
    """Apply actions from the Triage sheet of an already-loaded workbook."""
    ws = wb["Triage"]

    # Read headers from row 1
    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, ws.max_column + 1)]

    idx_id     = _col_index(headers, "_id")
    idx_num    = _col_index(headers, "#")
    idx_action = _col_index(headers, "Triage Action") or _col_index(headers, "Action")
    idx_notes  = _col_index(headers, "Notes")

    if idx_id is None or idx_action is None:
        raise ValueError("Required columns '_id' or 'Action' not found. Was the file modified?")

    done = dropped = snoozed = skipped = errored = 0
    results = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        loop_id = str(row[idx_id] or "").strip()
        action  = str(row[idx_action] or "").strip().lower()
        notes   = str(row[idx_notes] or "").strip() if idx_notes is not None else ""
        num     = row[idx_num] if idx_num is not None else "?"

        if not loop_id:
            skipped += 1
            continue

        try:
            # Save notes first (for any action, including blank)
            if notes:
                existing = ledger.get_loop(loop_id)
                if existing:
                    combined = ((existing.get("notes") or "") + f"\n{notes}").strip()
                    ledger.patch_loop(loop_id, notes=combined)

            if not action:
                if notes:
                    print(f"  #{num} note saved")
                skipped += 1
                continue

            if action == "done":
                loop_rec = ledger.get_loop(loop_id)
                ledger.resolve_loop(loop_id, "done")
                if _archive_in_front(loop_rec, num):
                    ledger.patch_loop(loop_id, front_archived=True)
                print(f"  #{num} done")
                done += 1

            elif action == "drop":
                loop_rec = ledger.get_loop(loop_id)
                ledger.resolve_loop(loop_id, "dropped")
                if _archive_in_front(loop_rec, num):
                    ledger.patch_loop(loop_id, front_archived=True)
                print(f"  #{num} dropped")
                dropped += 1

            elif action == "exclude":
                loop_rec = ledger.get_loop(loop_id)
                ledger.patch_loop(loop_id, category="junk")
                ledger.resolve_loop(loop_id, "dropped", reason="excluded:junk")
                if _archive_in_front(loop_rec, num):
                    ledger.patch_loop(loop_id, front_archived=True)
                print(f"  #{num} excluded (junk)")
                dropped += 1

            elif action == "subscribe":
                loop_rec = ledger.get_loop(loop_id)
                if loop_rec and loop_rec.get("channel") == "front":
                    try:
                        _get_front().add_tag(loop_rec["source_ref"], "cos/reading-list")
                        print(f"  #{num} tagged in Front as cos/reading-list")
                    except Exception as exc:
                        print(f"  #{num} WARNING: could not tag in Front: {exc}")
                ledger.resolve_loop(loop_id, "dropped", reason="subscribed:reading-list")
                print(f"  #{num} subscribed")
                dropped += 1

            elif action == "fyi":
                ledger.patch_loop(loop_id, fyi=True, deferred=False)
                print(f"  #{num} marked FYI")
                done += 1

            elif action == "defer":
                ledger.patch_loop(loop_id, deferred=True)
                print(f"  #{num} deferred")
                skipped += 1  # not resolved — just moved to deferred section

            elif action.startswith("snooze"):
                until = _parse_snooze_until(action)
                if not until:
                    print(f"  #{num} ERROR: can't parse snooze date from '{action}'")
                    errored += 1
                    continue
                ledger.snooze_loop(loop_id, until)
                print(f"  #{num} snoozed until {until[:10]}")
                snoozed += 1

            else:
                print(f"  #{num} unknown action '{action}' — skipped")
                skipped += 1

        except Exception as exc:
            print(f"  #{num} ERROR: {exc}")
            errored += 1

    print(f"\nTriage: Done: {done}  Dropped: {dropped}  Snoozed: {snoozed}  "
          f"Skipped: {skipped}  Errors: {errored}")
    return {
        "done": done, "dropped": dropped, "snoozed": snoozed,
        "skipped": skipped, "errored": errored,
        "total_actioned": done + dropped + snoozed,
    }


def _import_sender_rules(wb: openpyxl.Workbook) -> dict:
    if "Sender Rules" not in wb.sheetnames:
        return {"upserted": 0, "deleted": 0}
    ws = wb["Sender Rules"]
    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, ws.max_column + 1)]

    def _ci(name):
        try: return headers.index(name)
        except ValueError: return None

    idx_email   = _ci("Email / Domain")
    idx_action  = _ci("Action")
    idx_cat     = _ci("Category")
    idx_dir     = _ci("Direction")
    idx_imp     = _ci("Importance")
    idx_subj    = _ci("Subject Pattern")
    idx_notes   = _ci("Notes")
    idx_del     = _ci("_delete")

    if idx_email is None or idx_action is None:
        print("  Sender Rules sheet: missing required columns — skipped")
        return {"upserted": 0, "deleted": 0}

    upserted = deleted = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        email = str(row[idx_email] or "").strip().lower()
        if not email:
            continue
        delete_flag = str(row[idx_del] or "").strip().lower() == "yes" if idx_del is not None else False
        if delete_flag:
            if ledger.delete_sender_rule(email):
                print(f"  sender-rule deleted: {email}")
                deleted += 1
            continue
        action = str(row[idx_action] or "").strip().lower()
        if not action:
            continue
        imp_raw = row[idx_imp] if idx_imp is not None else None
        try:
            imp = int(float(str(imp_raw))) if imp_raw else 0
        except (ValueError, TypeError):
            imp = 0
        ledger.upsert_sender_rule(
            email=email, action=action,
            category=str(row[idx_cat] or "").strip() if idx_cat is not None else "",
            direction=str(row[idx_dir] or "").strip() if idx_dir is not None else "",
            importance=imp,
            subject_pattern=str(row[idx_subj] or "").strip() if idx_subj is not None else "",
            notes=str(row[idx_notes] or "").strip() if idx_notes is not None else "",
        )
        print(f"  sender-rule upserted: {email} -> {action}")
        upserted += 1

    return {"upserted": upserted, "deleted": deleted}


def _import_guidance(wb: openpyxl.Workbook) -> dict:
    if "Guidance" not in wb.sheetnames:
        return {"upserted": 0, "deleted": 0}
    ws = wb["Guidance"]
    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, ws.max_column + 1)]

    def _ci(name):
        try: return headers.index(name)
        except ValueError: return None

    idx_key    = _ci("Key")
    idx_scope  = _ci("Scope")
    idx_body   = _ci("Body")
    idx_active = _ci("Active")
    idx_del    = _ci("_delete")

    if idx_key is None or idx_body is None:
        print("  Guidance sheet: missing required columns — skipped")
        return {"upserted": 0, "deleted": 0}

    upserted = deleted = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        key = str(row[idx_key] or "").strip().lower()
        if not key:
            continue
        delete_flag = str(row[idx_del] or "").strip().lower() == "yes" if idx_del is not None else False
        if delete_flag:
            if ledger.delete_guidance(key):
                print(f"  guidance deleted: {key}")
                deleted += 1
            continue
        body = str(row[idx_body] or "").strip()
        if not body:
            continue
        scope  = str(row[idx_scope] or "all").strip() if idx_scope is not None else "all"
        active_raw = str(row[idx_active] or "yes").strip().lower() if idx_active is not None else "yes"
        active = active_raw not in ("no", "false", "0")
        ledger.upsert_guidance(key=key, body=body, scope=scope or "all", active=active)
        print(f"  guidance upserted: {key} (active={active})")
        upserted += 1

    return {"upserted": upserted, "deleted": deleted}


def run_import(xlsx_path: str | None = None) -> dict:
    # Re-named old run_import to _run_triage_import; wrapper calls both
    return _run_all_imports(xlsx_path)


def _run_all_imports(xlsx_path: str | None = None) -> dict:
    if not xlsx_path:
        latest = _find_latest_export()
        if not latest:
            raise FileNotFoundError("No triage file found in data/triage/. Run export first.")
        xlsx_path = str(latest)

    print(f"Reading: {xlsx_path}")
    wb = openpyxl.load_workbook(xlsx_path)

    # ── Triage sheet ─────────────────────────────────────────────────────────
    triage_result = _run_triage_sheet(wb)

    # ── Sender Rules sheet ────────────────────────────────────────────────────
    sr = _import_sender_rules(wb)
    if sr["upserted"] or sr["deleted"]:
        print(f"\nSender Rules: {sr['upserted']} upserted, {sr['deleted']} deleted")

    # ── Guidance sheet ────────────────────────────────────────────────────────
    gd = _import_guidance(wb)
    if gd["upserted"] or gd["deleted"]:
        print(f"Guidance: {gd['upserted']} upserted, {gd['deleted']} deleted")

    remaining = len(ledger.list_loops())
    print(f"Active loops remaining in Firestore: {remaining}")
    return {**triage_result, "sender_rules": sr, "guidance": gd}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    _run_all_imports(path)
