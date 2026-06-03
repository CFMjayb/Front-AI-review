"""Export active CoS loops to an Excel triage spreadsheet.

Usage:
    python cos_triage_export.py [output_path]

If output_path is omitted, writes to:
    data/triage/CoS Triage YYYY-MM-DD.xlsx

Columns:
    #       Stable loop number — used by the import script to match rows
    Dir     i_owe (On You) or owed_to_me (Waiting)
    Counterparty
    Summary
    Category
    Due
    First Seen
    Link    Deep link back to Front
    Action  Fill in: done / drop / snooze YYYY-MM-DD / snooze 1w / snooze 2d
    Notes   Free text — stored on the loop record when imported
    _id     Loop ID (do not edit — used by importer)
"""
import datetime
import os
import sys
from pathlib import Path

os.environ.setdefault("LEDGER_BACKEND", os.environ.get("LEDGER_BACKEND", "firestore"))
os.environ.setdefault("GCP_PROJECT", os.environ.get("GCP_PROJECT", "cfm-front-mail"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from cos import ledger
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

# ── Colours ──────────────────────────────────────────────────────────────────
RED    = "FFFCE4E4"   # i_owe / on-you rows
YELLOW = "FFFFF9E6"   # owed_to_me / waiting rows
GREY   = "FFF5F5F5"   # FYI rows
HEADER = "FF2C5F8A"   # header background (navy)
WHITE  = "FFFFFFFF"

THIN = Side(style="thin", color="FFD0D0D0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

COLS = [
    ("#",           5),
    ("Dir",         10),
    ("Counterparty", 22),
    ("Summary",     60),
    ("Category",    14),
    ("Due",         12),
    ("First Seen",  12),
    ("Link",        12),
    ("Action",      22),
    ("Notes",       35),
    ("_id",         0),   # hidden — do not delete; used by importer
]


def _local_today() -> str:
    tz_name = os.environ.get("COS_TIMEZONE", "America/New_York")
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
        return datetime.datetime.now(tz).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def _row_fill(loop: dict) -> str:
    if loop.get("fyi"):
        return GREY
    if loop.get("direction") == "i_owe":
        return RED
    return YELLOW


def export(output_path: str | None = None) -> str:
    today = _local_today()
    if not output_path:
        out_dir = Path(__file__).parent / "data" / "triage"
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(out_dir / f"CoS Triage {today}.xlsx")

    loops = ledger.list_loops()

    # Sort: i_owe (non-FYI) first by importance desc, then owed_to_me, then FYI
    def sort_key(l):
        fyi = 1 if l.get("fyi") else 0
        dir_order = 0 if l["direction"] == "i_owe" else 1
        importance = -(l.get("importance") or 3)
        due = l.get("due_at") or "9999"
        return (fyi, dir_order, importance, due)

    loops.sort(key=sort_key)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Triage"
    ws.freeze_panes = "A2"

    # ── Header row ───────────────────────────────────────────────────────────
    hdr_font   = Font(bold=True, color="FFFFFFFF", size=11)
    hdr_fill   = PatternFill("solid", fgColor=HEADER)
    hdr_align  = Alignment(horizontal="center", vertical="center", wrap_text=False)

    for col_idx, (name, _) in enumerate(COLS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = hdr_align
        cell.border = BORDER

    ws.row_dimensions[1].height = 20

    # ── Data rows ────────────────────────────────────────────────────────────
    for row_idx, loop in enumerate(loops, start=2):
        fill = PatternFill("solid", fgColor=_row_fill(loop))
        num        = loop.get("num") or ""
        direction  = "On You" if loop["direction"] == "i_owe" else "Waiting"
        if loop.get("fyi"):
            direction = "FYI"
        due = (loop.get("due_at") or "")[:10]
        first_seen = (loop.get("first_seen") or "")[:10]
        link = loop.get("source_link") or ""

        values = [
            num,
            direction,
            loop.get("counterparty") or "",
            loop.get("summary") or "",
            loop.get("category") or "",
            due,
            first_seen,
            link,
            "",   # Action — user fills in
            "",   # Notes  — user fills in
            loop.get("id") or "",
        ]

        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.fill = fill
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=(col_idx == 4))
            if col_idx == 4:  # Summary — taller row
                ws.row_dimensions[row_idx].height = 40
            if col_idx == 8 and val:  # Link — hyperlink
                cell.hyperlink = val
                cell.value = "open"
                cell.font = Font(color="FF0563C1", underline="single")

    # ── Action column dropdown ────────────────────────────────────────────────
    action_col = next(i for i, (n, _) in enumerate(COLS, 1) if n == "Action")
    dv = DataValidation(
        type="list",
        formula1='"done,drop,snooze 1d,snooze 3d,snooze 1w,snooze 2w,snooze 1m"',
        allow_blank=True,
        showDropDown=False,
    )
    dv.sqref = f"{get_column_letter(action_col)}2:{get_column_letter(action_col)}{len(loops)+1}"
    ws.add_data_validation(dv)

    # ── Column widths + hide _id ─────────────────────────────────────────────
    for col_idx, (name, width) in enumerate(COLS, start=1):
        col_letter = get_column_letter(col_idx)
        if width == 0:
            ws.column_dimensions[col_letter].hidden = True
        else:
            ws.column_dimensions[col_letter].width = width

    # ── Summary row at top (row 1 is headers; insert info in sheet tab) ──────
    ws.sheet_properties.tabColor = "2C5F8A"

    # ── Instructions sheet ───────────────────────────────────────────────────
    info = wb.create_sheet("Instructions")
    instructions = [
        ("CoS Triage Spreadsheet", True),
        ("", False),
        (f"Generated: {today}  |  {len(loops)} active loops", False),
        ("", False),
        ("HOW TO USE:", True),
        ("1. Review the Triage sheet. Rows are colour-coded:", False),
        ("   Red   = On You (i_owe) — you owe someone a response or action", False),
        ("   Yellow = Waiting  — you're waiting on someone else", False),
        ("   Grey  = FYI       — informational, auto-clears in 24h", False),
        ("", False),
        ("2. Fill in the Action column for any loop you want to resolve:", False),
        ("   done          — mark the loop as completed", False),
        ("   drop          — discard it (won't appear again)", False),
        ("   snooze 1d     — hide for 1 day", False),
        ("   snooze 1w     — hide for 1 week", False),
        ("   snooze 2w     — hide for 2 weeks", False),
        ("   snooze 1m     — hide for 1 month", False),
        ("   snooze YYYY-MM-DD  — hide until a specific date", False),
        ("   (leave blank)      — no change", False),
        ("", False),
        ("3. Optionally add a Note — it will be saved on the loop record.", False),
        ("", False),
        ("4. Save the file and run:  Run CoS Triage.bat  → option 2 (Import)", False),
        ("", False),
        ("DO NOT delete or edit the _id column — the importer uses it to match rows.", False),
    ]
    for text, bold in instructions:
        row = info.append([text])
        if bold:
            info.cell(info.max_row, 1).font = Font(bold=True, size=12)
    info.column_dimensions["A"].width = 70

    wb.save(output_path)
    print(f"Exported {len(loops)} loops -> {output_path}")
    return output_path


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    export(path)
