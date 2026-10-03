"""Archive a Front conversation when its loop leaves the triage list.

Anything that takes a loop off the list must also close the thread in Front —
otherwise the item disappears from triage while still sitting unread in the
mailbox, which is worse than leaving it on the list. That applies to the triage
importer (done / drop / exclude), the retirement scripts, and the pipeline's
skip paths for excluded senders.

This is the single implementation. It was originally inline in
cos_triage_import.py; it lives here so every caller behaves identically.

2026-10-02: added a 429 retry (Front's client sleeps Retry-After and then RAISES,
so a one-shot caller used to give up and leave a closed loop open in Front for
good) and the newer-inbound guard used by the closed-loop backlog cleanup and the
pipeline sweep (see "Remediation Plan - Archive Closed Loops").
"""
import calendar
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Front has no literal "open" status — open means assigned or unassigned.
OPEN_STATUSES = {"open", "assigned", "unassigned"}

# Verdicts from classify_for_archive().
ARCHIVE = "archive"   # still open in Front and nothing new since we closed it
STAMP = "stamp"       # Front already agrees (archived / deleted / spam / gone)
HOLD = "hold"         # open, but the sender wrote AFTER we closed the loop


def call_with_429_retry(fn: Callable, *args, attempts: int = 4, **kwargs):
    """Call a FrontClient method, retrying on 429.

    FrontClient._request sleeps the server's Retry-After and then raises
    FrontApiError(429), so a retry here only has to call again — the wait has
    already happened. Any other error (and the last 429) propagates unchanged.
    """
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if getattr(exc, "status", None) == 429 and i < attempts - 1:
                continue
            raise


def _is_transient(exc: Exception) -> bool:
    """Timeouts, dropped connections and gateway errors: worth another try."""
    status = getattr(exc, "status", None)
    if status in (502, 503, 504):
        return True
    if status is None and isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    # FrontClient wraps URLError as FrontApiError(None, "Could not reach Front: ...")
    return status is None and "Could not reach Front" in str(exc)


def call_with_retry(fn: Callable, *args, attempts: int = 5, backoff: float = 3.0, **kwargs):
    """Like call_with_429_retry, but also retries transient network/gateway errors
    with a growing pause. Used by the bulk scripts, where one 30s Front read
    timeout must not kill an hours-long pass."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = i == attempts - 1
            if getattr(exc, "status", None) == 429 and not last:
                continue                      # the client already slept Retry-After
            if _is_transient(exc) and not last:
                time.sleep(backoff * (i + 1))
                continue
            raise


class Throttle:
    """Keep consecutive Front requests at least `interval` seconds apart (the
    bulk scripts run at ~1 request/second, the rate proven not to trip 429s)."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        gap = self.interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def newer_inbound_since(conv: dict, resolved_iso: Optional[str]) -> bool:
    """True if the conversation has an unreplied inbound message newer than the
    moment the loop was closed.

    Front's `waiting_since` (epoch seconds) is when the conversation started
    waiting on us. Archiving a conversation in that state would bury a fresh
    reply, so callers hold those back. Without a known resolution time we cannot
    tell, so we answer False and let the caller decide (callers that cannot
    establish a resolution time should treat that as HOLD themselves).
    """
    ws = (conv or {}).get("waiting_since")
    rt = _iso_to_epoch(resolved_iso)
    if not ws or rt is None:
        return False
    try:
        return float(ws) > rt
    except (TypeError, ValueError):
        return False


def classify_for_archive(conv: Optional[dict], resolved_iso: Optional[str]) -> str:
    """ARCHIVE / STAMP / HOLD for a closed loop's conversation.

    conv=None means Front says the conversation does not exist (404) -> STAMP.
    """
    if conv is None:
        return STAMP
    status = conv.get("status") or ""
    if status not in OPEN_STATUSES:
        return STAMP
    if newer_inbound_since(conv, resolved_iso):
        return HOLD
    return ARCHIVE


def archive_conversation(front: Any, source_ref: str, *,
                         label: str = "", printer=None) -> bool:
    """Archive one Front conversation. Returns True if the ledger should stamp
    front_archived=True.

    Checks the current status first, so this is idempotent and cheap to re-run:
      - already archived / spam / deleted -> nothing to do, True
      - 404 (gone from Front)             -> True
      - still open                        -> PATCH to archived, True
      - any other error                   -> warn, False (status unknown)

    Rate limits (429) are retried a few times before giving up.

    Never raises: a Front problem must not block the ledger update that the
    caller has already decided on.
    """
    say = printer or (lambda msg: logger.info(msg))
    if not source_ref:
        return False

    try:
        conv = call_with_429_retry(front.get_conversation, source_ref)
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
        call_with_429_retry(front.set_status, source_ref, "archived")
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
