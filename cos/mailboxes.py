"""Mailbox registry — which mailboxes the Chief of Staff tracks, and how an open
loop is attributed to one of them.

One mailbox = one or more Front inboxes (an address with aliases has several)
             = one triage spreadsheet
             = one section of the morning email.

This module is the single source of truth for that list. Adding a mailbox here is
the only edit needed: the pipeline starts scanning its inbox, new loops get
stamped with its key, the export writes another workbook, and the briefing grows
another section. Nothing else hardcodes a mailbox.

Attribution rule — the **To: field**. A loop belongs to the mailbox whose address
the mail was sent to, regardless of who sent it. Addressed to two of Jay's
addresses? It belongs to BOTH mailboxes and appears on both spreadsheets, so a
loop carries a LIST of keys, not one.

Cc does not count — only To.

The Front inbox is a **fallback only**, for conversations where no To: address
matches: a BCC, a forward, or a non-email channel (SMS, Slack) that has no
recipient handle at all. Anything still unresolved lands in the UNASSIGNED bucket
rather than being silently dropped.

History worth keeping: the first version of this attributed purely by Front inbox.
That is subtly different and wrong — mail from EDOM's business office sent to
jay@cfmins.org is filed by Front in the CFM inbox, which is correct for "which
mailbox did this arrive in" but not for "which of my addresses was it sent to"
once a thread involves more than one of them.
"""
import os

UNASSIGNED = "other"
UNASSIGNED_LABEL = "Unattributed"

# Attribution is by the **To: field** — which of Jay's addresses the mail was sent
# to. Jay, 2026-08-18: "if an email comes in to jay@cfmins.org it goes on CFM. if
# an email comes in to jboggs@episcopalmaryland.org it is on EDOM — this has
# nothing to do with who the email is from." Cc does NOT count (his call), and a
# message addressed to two of his addresses belongs to BOTH mailboxes, so a loop
# can carry more than one key.

# ── The registry ─────────────────────────────────────────────────────────────
# key       short, stable, used in the loop record + filenames. Never rename a
#           key without backfilling the loops that carry it.
# label     human name — sheet title, email section heading, workbook filename.
# address   the address a human would call this mailbox. Display only.
# inbox_ids Front inbox IDs that feed this mailbox. The attribution key.
# scan      True  → the pipeline fetches this inbox and pays for AI review of it.
#           False → registered (so it gets a spreadsheet + section) but not
#                   fetched. Use for a mailbox that is not yet connected to
#                   Front, or one deliberately parked.
MAILBOXES: list[dict] = [
    {
        "key": "cfm",
        "label": "Jay — CFM",
        "address": "jay@cfmins.org",
        "addresses": ["jay@cfmins.org"],
        "inbox_ids": ["inb_csx96"],
        "scan": True,
    },
    {
        "key": "edom",
        "label": "Jay — EDOM",
        "address": "jboggs@episcopalmaryland.org",
        "addresses": ["jboggs@episcopalmaryland.org"],
        "inbox_ids": ["inb_cv4ii"],
        "scan": True,
    },
    # Registered on 2026-08-18 with scan=False, which needs explaining.
    #
    # This inbox is NOT in the pipeline's inbox scan list, so the first read of
    # the config says it has no loops. It has 29. They arrive by a second route:
    # the TEAMMATE_IDS secret is tea_byq3e (Jay), so the pipeline also fetches
    # every conversation ASSIGNED to Jay regardless of which inbox it lives in.
    # DME finance conversations assigned to him land in the ledger that way.
    #
    # So registering it costs nothing — those loops are already being fetched and
    # already paid for. Without an entry here they pile up in the UNASSIGNED
    # bucket and get a workbook labelled "Unattributed", which is the same file
    # count with a worse label. scan stays False because turning it True is a
    # genuinely different decision: it would pull the WHOLE shared finance queue,
    # not just Jay's assigned items, and pay for AI review of all of it.
    {
        "key": "dme",
        "label": "DME Finance",
        "address": "finance@episcopalmaine.org",
        # This inbox RECEIVES as ...@episcopalmaine.net and SENDS AS ...org, so
        # both spellings must count or its mail lands nowhere.
        "addresses": ["finance@episcopalmaine.org", "finance@episcopalmaine.net"],
        "inbox_ids": ["inb_cr72y"],
        "scan": False,
    },
]


def mailboxes(*, include_unassigned: bool = False) -> list[dict]:
    """Registered mailboxes in display order."""
    out = list(MAILBOXES)
    if include_unassigned:
        out.append({"key": UNASSIGNED, "label": UNASSIGNED_LABEL, "address": "",
                    "inbox_ids": [], "scan": False})
    return out


def keys(*, include_unassigned: bool = False) -> list[str]:
    return [m["key"] for m in mailboxes(include_unassigned=include_unassigned)]


def by_key(key: str) -> dict | None:
    key = (key or "").strip().lower()
    if key == UNASSIGNED:
        return {"key": UNASSIGNED, "label": UNASSIGNED_LABEL, "address": "",
                "inbox_ids": [], "scan": False}
    for m in MAILBOXES:
        if m["key"] == key:
            return m
    return None


def label_for(key: str) -> str:
    m = by_key(key)
    return m["label"] if m else UNASSIGNED_LABEL


def address_for(key: str) -> str:
    m = by_key(key)
    return m.get("address", "") if m else ""


def key_for_inbox(inbox_id: str) -> str:
    """Map a Front inbox ID to a mailbox key, or UNASSIGNED if not registered."""
    if not inbox_id:
        return UNASSIGNED
    for m in MAILBOXES:
        if inbox_id in m["inbox_ids"]:
            return m["key"]
    return UNASSIGNED


def key_for_inboxes(inbox_ids: list[str]) -> str:
    """First registry match wins — see the attribution note in the module docstring."""
    ids = set(inbox_ids or [])
    for m in MAILBOXES:
        if ids.intersection(m["inbox_ids"]):
            return m["key"]
    return UNASSIGNED


def addresses_for(key: str) -> list[str]:
    m = by_key(key) or {}
    return [a.lower() for a in m.get("addresses", [])]


def keys_on_loop(loop: dict) -> list[str]:
    """Every mailbox key a stored loop belongs to.

    Prefers the `mailboxes` list field (post-To:-rule loops, possibly several
    keys). Falls back to the single `mailbox` field for loops written before
    that field existed. [UNASSIGNED] when neither is set, so callers can treat
    "no mailbox" as a real membership rather than a missing field.
    """
    keys = loop.get("mailboxes")
    if keys:
        return list(keys)
    single = loop.get("mailbox")
    return [single] if single else [UNASSIGNED]


def keys_for_recipients(to_handles) -> list[str]:
    """Mailbox keys for a message's To: handles — the primary attribution rule.

    Returns EVERY mailbox matched, in registry order, because a message addressed
    to two of Jay's addresses belongs on both spreadsheets (his explicit call).
    Returns [] when no address matches, which is a real answer — the caller falls
    back to the Front inbox rather than guessing.

    Pass To: handles only. Cc does not count.
    """
    got = {(h or "").strip().lower() for h in (to_handles or [])}
    got.discard("")
    out: list[str] = []
    for m in MAILBOXES:
        if got.intersection(a.lower() for a in m.get("addresses", [])):
            out.append(m["key"])
    return out


def scan_inbox_ids() -> list[str]:
    """Inbox IDs the pipeline should fetch.

    Registry entries with scan=True, unioned with anything still listed in the
    INBOX_IDS env var. The union is deliberate: it keeps a deployed Cloud Run job
    working if its env var is ahead of, or behind, this file — an inbox never
    silently stops being scanned because of a deploy-ordering mistake.
    """
    ids: list[str] = []
    for m in MAILBOXES:
        if m.get("scan"):
            for iid in m["inbox_ids"]:
                if iid not in ids:
                    ids.append(iid)
    for iid in (i.strip() for i in os.environ.get("INBOX_IDS", "").split(",")):
        if iid and iid not in ids:
            ids.append(iid)
    return ids


def slug(key: str) -> str:
    """Filename-safe fragment for a mailbox's workbook."""
    label = label_for(key)
    keep = [c if (c.isalnum() or c in " -_") else "-" for c in label]
    return "".join(keep).strip().replace("  ", " ")
