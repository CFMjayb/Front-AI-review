"""M3 — Front loop extraction (adapter over the channel-agnostic core).

Normalizes Front conversations into the shared thread shape and delegates the
direction logic and reconcile to cos/extract.py. Reuses the analysis the pipeline
already paid for, so there's no extra Claude cost.
"""
import logging

from cos import extract, ledger
from front_client import FrontClient

logger = logging.getLogger(__name__)


def front_source_link(conversation_id: str) -> str:
    return f"https://app.frontapp.com/open/{conversation_id}"


def _from_and_to(m: dict) -> tuple[str, str, list[str]]:
    """Pull the real sender + 'to' handles from a Front message.

    Front puts the sender in recipients[role='from'] (and `author` is null on
    INBOUND messages — the sender is a contact, not a teammate). Reading `author`
    alone yields an empty sender for every inbound email, which is why loop
    counterparties came out 'unknown'.
    """
    from_name = from_email = ""
    tos: list[str] = []
    for r in (m.get("recipients") or []):
        role, handle, name = r.get("role"), r.get("handle") or "", r.get("name") or ""
        if role == "from" and not from_email:
            from_name, from_email = name, handle
        elif role == "to":
            tos.append(handle)
    return from_name, from_email, tos


def _normalize(messages: list[dict]) -> list[dict]:
    norm: list[dict] = []
    for m in messages or []:
        author = m.get("author") or {}
        author_name = f"{author.get('first_name') or ''} {author.get('last_name') or ''}".strip()
        from_name, from_email, tos = _from_and_to(m)
        sender_name = (from_name or author_name or from_email
                       or author.get("email") or author.get("handle") or "")
        sender_email = from_email or author.get("email") or ""
        if not tos:  # fallback for any non-standard shape
            tos = [r.get("handle", "") for r in (m.get("to") or [])]
        norm.append(extract.make_message(
            inbound=bool(m.get("is_inbound")),
            ts_epoch=m.get("created_at") or 0,
            sender_name=sender_name,
            sender_email=sender_email,
            recipients=tos,
            text=FrontClient.extract_plain_text_body(m),
        ))
    return norm


def extract_from_analysis(conv: dict, messages: list[dict], analysis: dict,
                          *, dry_run: bool = False) -> dict | None:
    """Turn a Front conversation's existing analysis into a loop (or None)."""
    conv_id = conv.get("id")
    thread = extract.build_thread(
        channel="front", source_ref=conv_id, subject=conv.get("subject") or "",
        source_link=front_source_link(conv_id), messages=_normalize(messages))
    return extract.loop_from_thread(thread, analysis, dry_run=dry_run)


# Front statuses that mean "Jay has dealt with this" → resolve the loop.
# (Open conversations are 'assigned' / 'unassigned'; everything else is handled.)
_DONE_STATUSES = {"archived", "deleted", "trashed"}


def reconcile_open_front_loops(front, *, dry_run: bool = False) -> dict:
    """Cheap, Claude-free pass over Front loops already in the ledger.

    Resolves a loop when either Jay replied (message-direction flip) OR he archived
    the conversation in Front — so action-only loops (bank approvals, BILL payments)
    clear when he archives the thread after acting, not just on a reply.
    """
    def fetch(conv_id: str) -> list[dict]:
        return _normalize(front.get_conversation_messages(conv_id))

    def is_done(conv_id: str) -> bool:
        status = (front.get_conversation(conv_id) or {}).get("status") or ""
        return status in _DONE_STATUSES

    return extract.reconcile(ledger.list_loops(channel="front"), fetch,
                             is_done=is_done, dry_run=dry_run)
