"""Reusable outbound-email layer for the Chief of Staff.

Many features need to send Jay a message — the daily briefing today, and nudges,
escalations, and approvals later. This module is the single send path so those
features never re-implement transport. Transports are pluggable: 'front' is wired
now; 'outlook' and 'smtp' slot in behind the same send() interface via
register_transport().

Email bodies are authored as markdown (that's what the briefing produces) and
converted to lightweight HTML here so every message renders well.
"""
import html
import logging
import os
import re
import requests

logger = logging.getLogger(__name__)


class SendError(RuntimeError):
    pass


# ── Markdown → minimal HTML (shared by every outbound email) ──────────────────

def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", text)
    return text


def to_html(md: str) -> str:
    """Convert the subset of markdown the briefing uses into HTML."""
    out: list[str] = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 6)  # '# ' → <h2>
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        if line.strip() == "---":
            close_list()
            out.append("<hr>")
            continue
        item = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", line)
        if item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(item.group(1))}</li>")
            continue
        close_list()
        out.append(f"<p>{_inline(line)}</p>")

    close_list()
    return "\n".join(out)


# ── Transports ────────────────────────────────────────────────────────────────

def _default_to() -> list[str]:
    return [a.strip() for a in os.environ.get("SENDER_TO", "").split(",") if a.strip()]


def _front_channel_id(front) -> str:
    channel_id = os.environ.get("SENDER_FRONT_CHANNEL_ID", "").strip()
    if channel_id:
        return channel_id
    for ch in front.list_channels():
        if ch.get("address") and ch.get("id"):
            return ch["id"]
    raise SendError("No Front channel available to send from "
                    "(set SENDER_FRONT_CHANNEL_ID).")


def _send_front(*, subject: str, body_md: str, to: list[str],
                attachments: list[dict] | None = None) -> dict:
    from auth import get_front_api_token
    from front_client import FrontClient
    if attachments:
        logger.warning("Front transport does not support attachments yet — "
                       "%d attachment(s) dropped", len(attachments))
    front = FrontClient(get_front_api_token())
    channel_id = _front_channel_id(front)
    result = front.send_message(channel_id, to=to, subject=subject,
                                body=to_html(body_md), text=body_md)
    return {"transport": "front", "channel_id": channel_id, "to": to,
            "id": result.get("id") if isinstance(result, dict) else None}


def _send_http(*, subject: str, body_md: str, to: list[str],
              attachments: list[dict] | None = None) -> dict:
    url = os.environ.get("EMAIL_MCP_URL", "").strip()
    key = os.environ.get("EMAIL_MCP_API_KEY", "").strip()
    if not url:
        raise SendError("EMAIL_MCP_URL not set.")
    if not key:
        raise SendError("EMAIL_MCP_API_KEY not set.")
    payload = {"to": ", ".join(to), "subject": subject,
              "body_html": to_html(body_md), "body_text": body_md}
    if attachments:
        payload["attachments"] = attachments
    resp = requests.post(
        url,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        json=payload,
        timeout=60,  # bumped from 30s — attachments increase payload size
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != "sent":
        raise SendError(f"Email server returned unexpected response: {result}")
    return {"transport": "http", "url": url, "to": to}


_TRANSPORTS = {"front": _send_front, "http": _send_http}


def register_transport(name: str, fn) -> None:
    """Register a transport: fn(*, subject, body_md, to) -> dict."""
    _TRANSPORTS[name] = fn


def available_transports() -> list[str]:
    return sorted(_TRANSPORTS)


def send(*, subject: str, body_md: str, to: list[str] | None = None,
         transport: str | None = None, attachments: list[dict] | None = None) -> dict:
    """Send an email. body_md is markdown; converted to HTML per transport.

    transport defaults to SENDER_TRANSPORT (or 'front'). to defaults to SENDER_TO.
    attachments: [{"name", "content_type", "content_base64"}, ...] — only the
    'http' transport (26-122 email server) currently sends these; other
    transports log a warning and drop them.
    """
    transport = transport or os.environ.get("SENDER_TRANSPORT", "front")
    to = to or _default_to()
    if not to:
        raise SendError("No recipient (set SENDER_TO or pass to=).")
    fn = _TRANSPORTS.get(transport)
    if fn is None:
        raise SendError(f"Unknown transport {transport!r}. "
                        f"Available: {available_transports()}")
    logger.info("Sending %r via %s to %s%s", subject, transport, ", ".join(to),
               f" with {len(attachments)} attachment(s)" if attachments else "")
    return fn(subject=subject, body_md=body_md, to=to, attachments=attachments)
