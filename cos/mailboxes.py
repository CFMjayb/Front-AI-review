"""Mailbox registry — which mailboxes the Chief of Staff tracks, and how an open
loop is attributed to one of them.

One mailbox = one or more Front inboxes (an address with aliases has several)
             = one triage spreadsheet
             = one section of the morning email.

This module is the single source of truth for that list. Adding a mailbox here is
the only edit needed: the pipeline starts scanning its inbox, new loops get
stamped with its key, the export writes another workbook, and the briefing grows
another section. Nothing else hardcodes a mailbox.

Attribution rule: a loop belongs to the mailbox whose Front inbox the source
conversation sits in — asked of Front directly, never inferred from the
recipient list (a conversation can be addressed to several of Jay's addresses
while living in exactly one inbox). A conversation in more than one registered
inbox is attributed to the first match in registry order, so the order below is
deliberate: most-specific/most-owned first.

Loops that predate this registry, or that come from an inbox not listed here,
land in the UNASSIGNED bucket rather than being silently dropped.
"""
import os

UNASSIGNED = "other"
UNASSIGNED_LABEL = "Unattributed"

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
        "inbox_ids": ["inb_csx96"],
        "scan": True,
    },
    {
        "key": "edom",
        "label": "Jay — EDOM",
        "address": "jboggs@episcopalmaryland.org",
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
        "address": "finance@episcopalmaine.org",   # receives as ...@episcopalmaine.net
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
