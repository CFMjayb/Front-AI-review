"""Plain-text hygiene for everything that crosses between the ledger and a
triage spreadsheet.

Two jobs, one implementation (so the workbook, the importer and the server can
never disagree about what "clean" means):

  clean_text(value)       Make free text plain and predictable: repair UTF-8
                          that was mis-decoded as Windows-1252 ("a-circumflex,
                          euro, quote" for an em dash), fold typographic
                          punctuation to ASCII, drop emoji / symbols / invisible
                          and control characters, collapse whitespace.

  normalize_action(value) Reduce a dropdown / action cell to a bare lowercase
                          token ("done", "snooze 1w", "assign to joe"). A cell
                          that arrives as '"done', 'Done.', '“done”' or
                          '-> done <-' all become 'done'. Anything that is not a
                          letter, digit, space, hyphen or colon is removed.

Accented LETTERS are kept (names such as "Jose" with an acute accent are real
data); only symbols, pictographs and invisible characters are removed.

Added 2026-10-02 after the Triage Action dropdown offered '"done' (VBA list
validation wrapped in extra quotes) and the importer skipped it as unknown.
"""
from __future__ import annotations

import re
import unicodedata

# ── mojibake repair ───────────────────────────────────────────────────────────
# UTF-8 bytes that were decoded as Windows-1252 show up as a lead character
# (U+00C2..U+00F4) followed by 1-3 "continuation" characters, each of which is
# what cp1252 shows for a byte in 0x80-0xBF.
_CP1252_CONT = (
    "\u0080-¿"
    "€‚ƒ„…†‡ˆ‰Š‹Œ"
    "Ž‘’“”•–—˜™š›"
    "œžŸ"
)
_MOJIBAKE_RUN = re.compile("[Â-ô][" + _CP1252_CONT + "]{1,3}")


def _run_to_bytes(run: str) -> bytes:
    out = bytearray()
    for ch in run:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) < 256:
                out += ch.encode("latin-1")
            else:
                raise
    return bytes(out)


def _fix_run(m: "re.Match[str]") -> str:
    run = m.group(0)
    try:
        return _run_to_bytes(run).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return run          # genuinely an accented letter + punctuation: leave it


def repair_mojibake(s: str) -> str:
    """Fix runs that decode cleanly as UTF-8; leave everything else alone."""
    for _ in range(2):      # double-encoded text needs two passes
        fixed = _MOJIBAKE_RUN.sub(_fix_run, s)
        if fixed == s:
            break
        s = fixed
    return s


# ── typographic folding ───────────────────────────────────────────────────────
_FOLD = {
    # dashes and minus
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-",
    0x2015: "-", 0x2212: "-",
    # single quotes / apostrophes / primes
    0x2018: "'", 0x2019: "'", 0x201a: "'", 0x201b: "'", 0x2032: "'",
    0x00b4: "'", 0x02bc: "'",
    # double quotes / guillemets
    0x201c: '"', 0x201d: '"', 0x201e: '"', 0x201f: '"', 0x2033: '"',
    0x00ab: '"', 0x00bb: '"',
    # ellipsis, bullets, middots
    0x2026: "...", 0x2022: "-", 0x00b7: "-", 0x2023: "-", 0x25e6: "-",
    0x25cf: "-", 0x25aa: "-", 0x2043: "-",
    # arrows
    0x2192: "->", 0x2190: "<-", 0x21d2: "=>", 0x2194: "<->",
    # misc
    0x00d7: "x", 0x00a9: "(c)", 0x00ae: "(R)", 0x2122: "(TM)",
}

_VARIATION_SELECTORS = set(range(0xFE00, 0xFE10))
_WS = re.compile(r"\s+")


def clean_text(value) -> str:
    """Plain, single-line, mostly-ASCII text. None -> ''."""
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    s = repair_mojibake(s)
    s = s.translate(_FOLD)
    s = unicodedata.normalize("NFKC", s)
    out = []
    for ch in s:
        o = ord(ch)
        if o in _VARIATION_SELECTORS:
            continue
        cat = unicodedata.category(ch)
        if cat in ("Zs", "Zl", "Zp") or ch in "\t\n\r\f\v":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue                       # control / invisible / unassigned
        elif cat == "So":
            continue                       # emoji, pictographs, dingbats
        elif cat in ("Sk", "Sm") and o > 127:
            continue                       # stray non-ASCII symbols / math
        else:
            out.append(ch)                 # letters, marks, digits, punctuation, $ etc.
    return _WS.sub(" ", "".join(out)).strip()


# ── action cells ──────────────────────────────────────────────────────────────
_ACTION_DISALLOWED = re.compile(r"[^a-z0-9 :\-]")


def normalize_action(value) -> str:
    """Bare lowercase token for a dropdown/action cell. None -> ''."""
    s = clean_text(value).lower()
    s = _ACTION_DISALLOWED.sub("", s)
    s = _WS.sub(" ", s)
    return s.strip(" -:")
