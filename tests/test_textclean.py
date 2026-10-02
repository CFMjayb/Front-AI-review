"""Text hygiene for the triage spreadsheet (2026-10-02).

Root cause that prompted this: the workbook's Triage Action dropdown offered
'"done' because the VBA wrapped the list in extra quotes, and the importer
skipped '"done' as an unknown action. These tests pin both halves: the
dropdown source is clean, and the importer tolerates junk anyway.
"""
import pathlib
import re

import openpyxl
import pytest

from cos.textclean import clean_text, normalize_action, repair_mojibake

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── clean_text ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (None, ""),
    ("", ""),
    ("plain text", "plain text"),
    ("a — b", "a - b"),                        # em dash
    ("1–2", "1-2"),                            # en dash
    ("wait…", "wait..."),                      # ellipsis
    ("“quoted” and it’s", '"quoted" and it\'s'),
    ("a b", "a b"),                            # NBSP
    ("a​b﻿c", "abc"),                     # zero-width / BOM
    ("done ✅", "done"),                        # emoji dropped
    ("\U0001F4EC Inbox • item", "Inbox - item"),
    ("go → here", "go -> here"),
    ("tab\there\nnewline", "tab here newline"),
    ("  lots   of   space  ", "lots of space"),
    ("José Muñoz", "José Muñoz"),  # accented LETTERS are kept
])
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


def test_mojibake_is_repaired():
    # What an em dash looks like after UTF-8 -> cp1252 mis-decoding.
    assert repair_mojibake("Deferred â€” Review Later") == "Deferred — Review Later"
    assert clean_text("CoS Triage Workbook â€” Controls") == "CoS Triage Workbook - Controls"
    assert clean_text("itâ€™s") == "it's"
    assert clean_text("cafÃ©") == "café"


def test_legit_accent_before_punctuation_is_not_corrupted():
    # 'e-acute' followed by a curly quote must NOT be treated as mojibake.
    assert clean_text("café”") == 'café"'


# ── normalize_action ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ('"done', "done"),                       # the actual bug
    ('snooze 1m"', "snooze 1m"),             # ...and its last-item twin
    ('"done"', "done"),
    ("“done”", "done"),
    ("  Done ", "done"),
    ("DONE.", "done"),
    ("done!", "done"),
    ("✅ done", "done"),
    ("-> done <-", "done"),
    ("done ", "done"),
    ("do​ne", "done"),
    ("Delegate  to   Admin", "delegate to admin"),
    ("snooze 2026-10-05", "snooze 2026-10-05"),
    ("assign to joe", "assign to joe"),
    (None, ""),
    ("", ""),
    ('""', ""),
])
def test_normalize_action(raw, expected):
    assert normalize_action(raw) == expected


def test_every_dropdown_option_is_a_fixed_point():
    """If an option the sheet offers changed when normalized, picking it would
    break the import. The list comes from the single source the server serves."""
    from cos_triage_export import _triage_action_list
    options = _triage_action_list().split(",")
    assert options and all(o == normalize_action(o) for o in options), options


# ── importer tolerates junk ───────────────────────────────────────────────────

class _FakeLedger:
    def __init__(self):
        self.calls = []

    def get_loop(self, loop_id):
        return {"id": loop_id, "channel": "none"}

    def resolve_loop(self, loop_id, status, reason=None):
        self.calls.append(("resolve", loop_id, status))

    def patch_loop(self, loop_id, **kw):
        self.calls.append(("patch", loop_id, tuple(sorted(kw))))

    def snooze_loop(self, loop_id, until):
        self.calls.append(("snooze", loop_id))


def test_importer_accepts_quoted_and_decorated_actions(monkeypatch):
    import cos_triage_import as imp

    fake = _FakeLedger()
    monkeypatch.setattr(imp, "ledger", fake)
    monkeypatch.setattr(imp, "_archive_in_front", lambda *a, **k: False)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Triage"
    ws.append(["#", "Triage Action", "Notes", "_id"])
    ws.append([1, '"done', "", "L1"])
    ws.append([2, 'snooze 1m"', "", "L2"])
    ws.append([3, "“DROP”", "", "L3"])
    ws.append([4, "  ", "", "L4"])

    result = imp._run_triage_sheet(wb)

    assert ("resolve", "L1", "done") in fake.calls
    assert ("snooze", "L2") in fake.calls
    assert ("resolve", "L3", "dropped") in fake.calls
    assert result["done"] == 1 and result["dropped"] == 1 and result["snoozed"] == 1
    assert result["errored"] == 0


# ── VBA regression guards ─────────────────────────────────────────────────────

def _bas_files():
    return sorted((ROOT / "VBA").glob("*.bas"))


def test_vba_modules_are_pure_ascii():
    """Non-ASCII in a .bas is imported as ANSI and shows up as mojibake
    ('a-circumflex, euro, quote') in message boxes and cells."""
    bad = {p.name: sorted({hex(b) for b in p.read_bytes() if b > 127}) for p in _bas_files()}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, bad


def test_vba_list_validations_are_not_wrapped_in_extra_quotes():
    """VBA wants Formula1:="a,b,c". Wrapping it in quotes makes Excel offer
    '"a' and 'c"' as the first and last options."""
    pat = re.compile(r'Formula1\s*:=\s*"""')
    offenders = [p.name for p in _bas_files() if pat.search(p.read_text(encoding="ascii", errors="ignore"))]
    assert not offenders, offenders
