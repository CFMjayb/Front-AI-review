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


def _normalize(messages: list[dict]) -> list[dict]:
    norm: list[dict] = []
    for m in messages or []:
        author = m.get("author") or {}
        name = f"{author.get('first_name') or ''} {author.get('last_name') or ''}".strip()
        norm.append(extract.make_message(
            inbound=bool(m.get("is_inbound")),
            ts_epoch=m.get("created_at") or 0,
            sender_name=name or author.get("email") or author.get("handle") or "",
            sender_email=author.get("email") or "",
            recipients=[r.get("handle", "") for r in (m.get("to") or [])],
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


def reconcile_open_front_loops(front, *, dry_run: bool = False) -> dict:
    """Cheap, Claude-free pass over Front loops already in the ledger."""
    def fetch(conv_id: str) -> list[dict]:
        return _normalize(front.get_conversation_messages(conv_id))

    return extract.reconcile(ledger.list_loops(channel="front"), fetch, dry_run=dry_run)
