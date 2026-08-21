"""
create_triage_workbook.py — Build CoS Triage Workbook (26-119)

Creates CoS Triage Workbook.xlsm with embedded VBA and the MCP API key
pre-loaded in the Config sheet so no manual setup is needed.

Run via: Run CoS Triage Workbook.bat
"""
import os
import sys
import pathlib
import shutil
import tempfile

from dotenv import load_dotenv

HERE = pathlib.Path(__file__).resolve().parent
load_dotenv(HERE / ".env", override=True)

try:
    import win32com.client
except ImportError:
    print("ERROR: pywin32 not installed. Run: pip install pywin32")
    sys.exit(1)

VBA_DIR  = HERE / "VBA"


def _workbook_name(mailbox: str) -> str:
    if not mailbox:
        return "CoS Triage Workbook.xlsm"
    from cos import mailboxes
    return f"CoS Triage Workbook - {mailboxes.slug(mailbox)}.xlsm"

BAS_FILES = [
    "modConfig.bas",
    "modApi.bas",
    "modTriage.bas",
    "modSenderRules.bas",
    "modGuidance.bas",
    "modBriefing.bas",
    "modInstall.bas",
]


def _get_api_key() -> str:
    """Read MCP_API_KEY from env / Secret Manager."""
    key = os.environ.get("MCP_API_KEY", "").strip()
    if key:
        return key
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = "projects/cfm-front-mail/secrets/mcp-api-key/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        key = resp.payload.data.decode("UTF-8").strip()
        print(f"  API key fetched from Secret Manager.")
        return key
    except Exception as exc:
        print(f"  WARNING: Could not fetch API key from Secret Manager: {exc}")
        print("  You can paste the key manually into the Config sheet after opening the workbook.")
        print("  Run: gcloud secrets versions access latest --secret=mcp-api-key --project=cfm-front-mail")
        return ""


def _rgb(r: int, g: int, b: int) -> int:
    """VBA's RGB() as a plain int — win32com has no built-in equivalent."""
    return r + (g << 8) + (b << 16)


def _write_instructions_sheet(wb, mailbox_label: str) -> None:
    ws = wb.Sheets.Add(After=wb.Sheets(wb.Sheets.Count))
    ws.Name = "Instructions"
    lines = [
        ("CoS Triage Workbook", True, 14),
        ("", False, 11),
        (f"Mailbox: {mailbox_label}" if mailbox_label else
         "Mailbox: All (unscoped)", False, 11),
        ("", False, 11),
        ("DAILY WORKFLOW:", True, 12),
        ("  1. Click 'Refresh Triage' to pull your current loops.", False, 11),
        ("  2. Fill in a Triage Action for any row you want to act on "
         "(dropdown in that column).", False, 11),
        ("  3. Optionally add a Notes entry — saved even with no action.", False, 11),
        ("  4. Click 'Upload for Processing' — saves a copy, uploads it, "
         "the server applies every action, then refreshes this sheet.", False, 11),
        ("  5. Click 'Send Briefing' any time for an on-demand summary email.", False, 11),
        ("", False, 11),
        ("See the 'Action Guide' tab for exactly what each Triage Action "
         "and Sender Rule Action does.", False, 11),
        ("", False, 11),
        ("SENDER RULES / GUIDANCE TABS:", True, 12),
        ("  Refresh / Save independently of the Triage sheet — pre-classify "
         "senders and add standing instructions for the AI review.", False, 11),
        ("", False, 11),
        ("IMPORTANT:", True, 12),
        ("  Do not delete the hidden Config sheet or the _id column on "
         "Triage — both are used internally.", False, 11),
    ]
    for i, (text, bold, size) in enumerate(lines, start=1):
        cell = ws.Cells(i, 1)
        cell.Value = text
        cell.Font.Bold = bold
        cell.Font.Size = size
    ws.Columns("A").ColumnWidth = 90
    ws.Tab.Color = _rgb(100, 100, 100)


def _write_action_guide_sheet(wb) -> None:
    """Same content as the .xlsx export's Action Guide tab (cos_triage_export
    .action_guide_data) — one source, rendered here via COM instead of
    openpyxl. Keeps the two workbook formats from describing the same action
    two different ways."""
    from cos_triage_export import action_guide_data
    data = action_guide_data()

    ws = wb.Sheets.Add(After=wb.Sheets(wb.Sheets.Count))
    ws.Name = "Action Guide"
    ws.Tab.Color = _rgb(107, 58, 166)

    HDR_FILL = _rgb(107, 58, 166)
    SECTION_FILL = _rgb(68, 68, 102)

    row = 1
    ws.Cells(row, 1).Value = "Triage Workbook — Action Guide"
    ws.Cells(row, 1).Font.Bold = True
    ws.Cells(row, 1).Font.Size = 14
    row += 1
    ws.Cells(row, 1).Value = ("What each dropdown option actually does, "
        "generated to match the real import code — not a hand-written "
        "summary that can drift out of date.")
    ws.Cells(row, 1).Font.Italic = True
    row += 2

    def section(text):
        nonlocal row
        for c in range(1, 4):
            cell = ws.Cells(row, c)
            cell.Value = text if c == 1 else ""
            cell.Interior.Color = SECTION_FILL
            cell.Font.Color = _rgb(255, 255, 255)
            cell.Font.Bold = True
        row += 1

    def header():
        nonlocal row
        for c, name in enumerate(("Action", "What It Does", "Notes / Reversibility"), 1):
            cell = ws.Cells(row, c)
            cell.Value = name
            cell.Interior.Color = HDR_FILL
            cell.Font.Color = _rgb(255, 255, 255)
            cell.Font.Bold = True
        row += 1

    def data_row(values, height=48):
        nonlocal row
        for c, val in enumerate(values, 1):
            cell = ws.Cells(row, c)
            cell.Value = val
            cell.WrapText = True
            cell.VerticalAlignment = -4160  # xlTop
        ws.Rows(row).RowHeight = height
        row += 1

    section('TRIAGE SHEET — "Triage Action" column (per-loop, main workflow)')
    header()
    for action, what, notes in data["triage"]:
        data_row([action, what, notes], height=28 if action == "(blank)" else 48)

    row += 1
    section('SENDER RULES SHEET — "Action" column (per-sender, runs BEFORE '
            "Claude — no AI cost)")
    header()
    for action, what, notes in data["sender_rules"]:
        data_row([action, what, notes], height=32)

    ws.Columns("A").ColumnWidth = 26
    ws.Columns("B").ColumnWidth = 78
    ws.Columns("C").ColumnWidth = 42


def _set_config(wb, key: str, value: str) -> None:
    """Write a key/value into the Config sheet of the open workbook."""
    for sheet in wb.Sheets:
        if sheet.Name == "Config":
            for row in range(1, 20):
                if str(sheet.Cells(row, 1).Value or "").strip() == key:
                    sheet.Cells(row, 2).Value = value
                    return
            # Key not found — append
            for row in range(1, 20):
                if not sheet.Cells(row, 1).Value:
                    sheet.Cells(row, 1).Value = key
                    sheet.Cells(row, 2).Value = value
                    return
            break


def build_workbook(mailbox: str = "", *, api_key: str = "") -> pathlib.Path:
    """Build one workbook, scoped to `mailbox` (a cos/mailboxes.py key) or
    unscoped (every mailbox mixed) if blank. Returns the final output path."""
    workbook_name = _workbook_name(mailbox)
    out_path = HERE / workbook_name

    temp_dir  = pathlib.Path(tempfile.mkdtemp(prefix="cos_triage_build_"))
    temp_path = temp_dir / workbook_name

    if out_path.exists():
        try:
            out_path.unlink()
        except PermissionError:
            print(f"ERROR: Cannot delete existing workbook — close it in Excel first:\n  {out_path}")
            sys.exit(1)

    xl = win32com.client.DispatchEx("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False

    try:
        wb = xl.Workbooks.Add()
        wb.SaveAs(str(temp_path), FileFormat=52)  # xlOpenXMLWorkbookMacroEnabled
        print(f"  Created: {temp_path}")

        for bas in BAS_FILES:
            bas_path = str(VBA_DIR / bas)
            if not os.path.exists(bas_path):
                print(f"  MISSING: {bas} — skipping")
                continue
            wb.VBProject.VBComponents.Import(bas_path)
            print(f"  Imported {bas}")

        wb.Save()

        print("  Running modInstall.RunInstall ...")
        # RunInstall's completion MsgBox needs a human click, which can never
        # happen under xl.Visible = False — suppress it here or this hangs
        # forever (same fix already applied to the 26-125 workbook builder).
        xl.Run("modInstall.SetSuppressCompletionMsgBox", True)
        xl.Run("modInstall.RunInstall")

        # Bake in the API key and the mailbox scope
        if api_key:
            _set_config(wb, "API_KEY", api_key)
            print("  API key written to Config sheet.")
        if mailbox:
            _set_config(wb, "Mailbox", mailbox)
            print(f"  Mailbox scope written to Config sheet: {mailbox}")

        # Excel.Workbooks.Add() always starts with a blank default sheet;
        # RunInstall only ever adds named sheets, never removes it.
        for s in list(wb.Sheets):
            if s.Name == "Sheet1" and s.UsedRange.Address == "$A$1":
                s.Delete()

        mailbox_label = ""
        if mailbox:
            from cos import mailboxes
            mb = mailboxes.by_key(mailbox)
            mailbox_label = mb["label"] if mb else mailbox
        _write_instructions_sheet(wb, mailbox_label)
        _write_action_guide_sheet(wb)
        print("  Instructions + Action Guide sheets written.")

        wb.Save()
        wb.Close(SaveChanges=True)

    finally:
        xl.DisplayAlerts = True
        xl.Quit()

    shutil.move(str(temp_path), str(out_path))
    shutil.rmtree(str(temp_dir), ignore_errors=True)
    print(f"  Done: {out_path}")
    return out_path


def main():
    """No args: build one workbook per registered, non-unassigned mailbox
    (cfm/edom/dme today) — matches how cos_triage_export.export_all() already
    scopes the plain-.xlsx exports. `--mailbox <key>` builds just one.
    `--unscoped` builds the single legacy all-mailboxes-mixed workbook."""
    args = sys.argv[1:]
    api_key = _get_api_key()

    if "--unscoped" in args:
        targets = [("", "All mailboxes")]
    elif "--mailbox" in args:
        i = args.index("--mailbox")
        key = args[i + 1] if i + 1 < len(args) else ""
        from cos import mailboxes
        mb = mailboxes.by_key(key)
        if not mb:
            sys.exit(f"Unknown mailbox {key!r}. Known: {', '.join(mailboxes.keys())}")
        targets = [(key, mb["label"])]
    else:
        from cos import mailboxes
        targets = [(mb["key"], mb["label"])
                   for mb in mailboxes.mailboxes(include_unassigned=False)]

    built = []
    for key, label in targets:
        print(f"\nBuilding workbook for: {label or 'All mailboxes'}")
        built.append(build_workbook(key, api_key=api_key))

    print(f"\n{'='*60}\nDone. {len(built)} workbook(s):")
    for p in built:
        print(f"  {p}")
    if not api_key:
        print("\nACTION NEEDED: open each workbook and paste your MCP API key into the")
        print("Config sheet (make it visible via Format > Sheet > Unhide, then re-hide).")


if __name__ == "__main__":
    main()
