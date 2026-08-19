"""Archive a Front conversation when its loop leaves the triage list.

Anything that takes a loop off the list must also close the thread in Front —
otherwise the item disappears from triage while still sitting unread in the
mailbox, which is worse than leaving it on the list. That applies to the triage
importer (done / drop / exclude), the retirement scripts, and the pipeline's
skip paths for excluded senders.

This is the single implementation. It was originally inline in
cos_triage_import.py; it lives here so every caller behaves identically.
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Front has no literal "open" status — open means assigned or unassigned.
OPEN_STATUSES = {"open", "assigned", "unassigned"}


def archive_conversation(front: Any, source_ref: str, *,
                         label: str = "", printer=None) -> bool:
    """Archive one Front conversation. Returns True if the ledger should stamp
    front_archived=True.

    Checks the current status first, so this is idempotent and cheap to re-run:
      - already archived / spam / deleted -> nothing to do, True
      - 404 (gone from Front)             -> True
      - still open                        -> PATCH to archived, True
      - any other error                   -> warn, False (status unknown)

    Never raises: a Front problem must not block the ledger update that the
    caller has already decided on.
    """
    say = printer or (lambda msg: logger.info(msg))
    if not source_ref:
        return False

    try:
        conv = front.get_conversation(source_ref)
        status = conv.get("status") or ""
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            say(f"    -> {label or source_ref}: not found in Front, stamping anyway")
            return True
        say(f"    WARNING: {label or source_ref}: could not read Front status: {exc}")
        return False

    if status not in OPEN_STATUSES:
        say(f"    -> {label or source_ref}: already {status!r} in Front")
        return True

    try:
        front.set_status(source_ref, "archived")
        say(f"    -> {label or source_ref}: archived in Front")
        return True
    except Exception as exc:
        say(f"    WARNING: {label or source_ref}: could not archive: {exc}")
        return False


def archive_loop(front: Any, loop: Optional[dict], *, printer=None) -> bool:
    """archive_conversation for a loop record. Front-channel loops only."""
    if not loop or loop.get("channel") != "front":
        return False
    return archive_conversation(front, loop.get("source_ref"),
                                label=f"#{loop.get('num')}", printer=printer)
