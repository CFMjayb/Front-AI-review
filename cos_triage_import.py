"""Import a reviewed CoS triage spreadsheet and apply actions to Firestore.

Usage:
    python cos_triage_import.py [path_to_xlsx]

If path_to_xlsx is omitted, picks the most recent file in data/triage/.

Action column values (case-insensitive):
    done                  — resolve loop as done
    drop                  — resolve loop as dropped
    snooze YYYY-MM-DD     — snooze until that date
    snooze 1d / 2d / 3d   — snooze for N days
    snooze 1w / 2w        — snooze for N weeks
    snooze 1m             — snooze for 1 month
    (blank)               — skip, no change
"""
import datetime
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("LEDGER_BACKEND", os.environ.get("LEDGER_BACKEND", "firestore"))
os.environ.setdefault("GCP_PROJECT", os.environ.get("GCP_PROJECT", "cfm-front-mail"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from cos import ledger
import openpyxl


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
    files = sorted(triage_dir.glob("CoS Triage *.xlsx"), reverse=True)
    return files[0] if files else None


def _col_index(headers: list[str], name: str) -> int | None:
    try:
        return headers.index(name)
    except ValueError:
        return None


def run_import(xlsx_path: str | None = None) -> dict:
    if not xlsx_path:
        latest = _find_latest_export()
        if not latest:
            raise FileNotFoundError("No triage file found in data/triage/. Run export first.")
        xlsx_path = str(latest)

    print(f"Reading: {xlsx_path}")
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Triage"]

    # Read headers from row 1
    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, ws.max_column + 1)]

    idx_id     = _col_index(headers, "_id")
    idx_num    = _col_index(headers, "#")
    idx_action = _col_index(headers, "Action")
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

        if not loop_id or not action:
            skipped += 1
            continue

        try:
            if action == "done":
                ledger.resolve_loop(loop_id, "done")
                if notes:
                    # Append note to existing loop notes
                    existing = ledger.get_loop(loop_id)
                    if existing:
                        combined = ((existing.get("notes") or "") + f"\n{notes}").strip()
                        ledger.upsert_loop(
                            direction=existing["direction"],
                            counterparty=existing["counterparty"],
                            summary=existing["summary"],
                            channel=existing["channel"],
                            source_ref=existing["source_ref"],
                            notes=combined,
                        )
                print(f"  #{num} done")
                done += 1

            elif action == "drop":
                ledger.resolve_loop(loop_id, "dropped")
                print(f"  #{num} dropped")
                dropped += 1

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

    summary = {
        "done": done, "dropped": dropped, "snoozed": snoozed,
        "skipped": skipped, "errored": errored,
        "total_actioned": done + dropped + snoozed,
    }
    print(f"\nDone: {done}  Dropped: {dropped}  Snoozed: {snoozed}  "
          f"Skipped (no action): {skipped}  Errors: {errored}")
    remaining = len(ledger.list_loops())
    print(f"Active loops remaining in Firestore: {remaining}")
    return summary


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    run_import(path)
