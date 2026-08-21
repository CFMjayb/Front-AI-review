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
