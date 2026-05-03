"""Front API client for EDOM email ops.

Extends front-mail-organizer/front_client.py with tag/draft/teammate methods.
Pure functions — each method is a future MCP tool.
"""
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

BASE_URL = "https://api2.frontapp.com"
PROCESSED_TAG = "AI/processed"


class FrontApiError(RuntimeError):
    def __init__(self, status: Optional[int], message: str) -> None:
        self.status = status
        super().__init__(message)


def _build_url(path_or_url: str, params: Optional[dict[str, Any]] = None) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        url = f"{BASE_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"
    return url


def _request(method: str, path_or_url: str, token: str, body: Optional[dict] = None,
             params: Optional[dict] = None) -> tuple[Any, dict[str, str]]:
    url = _build_url(path_or_url, params)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "edom-email-ops/0.1.0",
    }
    body_bytes = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        body_bytes = json.dumps(body).encode("utf-8")

    request = Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read()
            response_headers = {k.lower(): v for k, v in response.headers.items()}
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise FrontApiError(401, "Front rejected the token. Check FRONT_API_TOKEN.") from exc
        if exc.code == 403:
            raise FrontApiError(403, "Token valid but missing required scope.") from exc
        if exc.code == 429:
            retry_after = int(exc.headers.get("Retry-After", "5"))
            time.sleep(retry_after)
            raise FrontApiError(429, f"Front rate limit hit. Waited {retry_after}s.") from exc
        raise FrontApiError(exc.code, f"Front {exc.code}: {body_text[:500]}") from exc
    except URLError as exc:
        raise FrontApiError(None, f"Could not reach Front: {exc.reason}") from exc

    if not response_body:
        return None, response_headers
    if "application/json" not in response_headers.get("content-type", ""):
        return response_body, response_headers
    return json.loads(response_body.decode("utf-8")), response_headers


def _result_items(payload: Any) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("_results"), list):
        return payload["_results"]
    if isinstance(payload, list):
        return payload
    return []


def _next_page(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    pagination = payload.get("_pagination")
    if not isinstance(pagination, dict):
        return None
    nxt = pagination.get("next")
    return nxt if isinstance(nxt, str) and nxt else None


def _collect_pages(path: str, token: str, *, limit: int = 50, max_pages: int = 5,
                   params: Optional[dict] = None) -> list[dict]:
    items: list[dict] = []
    nxt: Optional[str] = path
    pages = 0
    first_params = {"limit": limit, **(params or {})}

    while nxt and pages < max_pages:
        page_params = first_params if pages == 0 else None
        payload, headers = _request("GET", nxt, token, params=page_params)
        items.extend(_result_items(payload))
        nxt = _next_page(payload)
        pages += 1

        remaining = headers.get("x-ratelimit-remaining")
        if remaining == "0":
            reset = int(headers.get("x-ratelimit-reset", "0") or "0")
            sleep_for = max(0, reset - int(time.time()))
            if sleep_for:
                logger.info(f"Front rate limit, sleeping {sleep_for}s")
                time.sleep(sleep_for)

    return items


class FrontClient:
    """Front API operations. One method = one future MCP tool."""

    def __init__(self, token: str):
        self.token = token

    # ── Reads ────────────────────────────────────────────────────────────────

    def list_inboxes(self, *, limit: int = 100, max_pages: int = 5) -> list[dict]:
        return _collect_pages("/inboxes", self.token, limit=limit, max_pages=max_pages)

    def list_channels(self, *, limit: int = 100, max_pages: int = 5) -> list[dict]:
        return _collect_pages("/channels", self.token, limit=limit, max_pages=max_pages)

    def list_teammates(self, *, email: Optional[str] = None) -> list[dict]:
        all_teammates = _collect_pages("/teammates", self.token, limit=100, max_pages=5)
        if email:
            return [t for t in all_teammates if (t.get("email") or "").lower() == email.lower()]
        return all_teammates

    def list_inbox_conversations(self, inbox_id: str, *, status: Optional[str] = None,
                                  since_ms: Optional[int] = None,
                                  limit: int = 50, max_pages: int = 5) -> list[dict]:
        params: dict[str, Any] = {}
        if status:
            params["q[statuses][]"] = status
        if since_ms:
            params["q[after]"] = since_ms // 1000
        return _collect_pages(f"/inboxes/{inbox_id}/conversations", self.token,
                              limit=limit, max_pages=max_pages, params=params)

    def list_assigned_conversations(self, teammate_id: str, *, since_ms: Optional[int] = None,
                                     status: Optional[str] = None,
                                     limit: int = 50, max_pages: int = 5) -> list[dict]:
        params: dict[str, Any] = {}
        if since_ms:
            params["q[after]"] = since_ms // 1000
        if status:
            params["q[statuses][]"] = status
        return _collect_pages(f"/teammates/{teammate_id}/conversations", self.token,
                              limit=limit, max_pages=max_pages, params=params)

    def get_conversation(self, conversation_id: str) -> dict:
        data, _ = _request("GET", f"/conversations/{conversation_id}", self.token)
        return data

    def get_conversation_messages(self, conversation_id: str, *, max_pages: int = 10) -> list[dict]:
        return _collect_pages(f"/conversations/{conversation_id}/messages", self.token,
                              limit=50, max_pages=max_pages)

    def list_conversation_tags(self, conversation_id: str) -> list[dict]:
        data, _ = _request("GET", f"/conversations/{conversation_id}", self.token)
        return data.get("tags", []) if isinstance(data, dict) else []

    def search_conversations(self, query: str, *, max_pages: int = 3) -> list[dict]:
        encoded = quote(query, safe="")
        return _collect_pages(f"/conversations/search/{encoded}", self.token,
                              limit=50, max_pages=max_pages)

    def get_conversation_comments(self, conversation_id: str) -> list[dict]:
        return _collect_pages(f"/conversations/{conversation_id}/comments", self.token,
                              limit=50, max_pages=5)

    def has_comment_with_prefix(self, conversation_id: str, prefix: str) -> bool:
        """Return True if any existing comment body starts with prefix."""
        try:
            comments = self.get_conversation_comments(conversation_id)
            return any((c.get("body") or "").startswith(prefix) for c in comments)
        except Exception:
            return False

    # ── Tags (process-cached) ────────────────────────────────────────────────

    _tag_cache: Optional[list[dict]] = None

    def list_tags(self) -> list[dict]:
        if self._tag_cache is None:
            self._tag_cache = _collect_pages("/tags", self.token, limit=100, max_pages=10)
        return self._tag_cache

    def clear_tag_cache(self) -> None:
        self._tag_cache = None

    def find_tag_by_name(self, name: str) -> Optional[dict]:
        return next((t for t in self.list_tags() if t.get("name") == name), None)

    def create_tag(self, name: str, *, highlight: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"name": name}
        if highlight:
            body["highlight"] = highlight
        data, _ = _request("POST", "/tags", self.token, body=body)
        self.clear_tag_cache()
        return data

    def ensure_tag(self, name: str, *, highlight: Optional[str] = None) -> dict:
        existing = self.find_tag_by_name(name)
        if existing:
            return existing
        logger.info(f"Creating missing tag: {name}")
        return self.create_tag(name, highlight=highlight)

    # ── Writes ───────────────────────────────────────────────────────────────

    def add_comment(self, conversation_id: str, body: str,
                    *, author_id: Optional[str] = None) -> dict:
        payload: dict[str, Any] = {"body": body}
        if author_id:
            payload["author_id"] = author_id
        data, _ = _request("POST", f"/conversations/{conversation_id}/comments",
                           self.token, body=payload)
        return data

    def add_tag(self, conversation_id: str, tag_name: str) -> dict:
        tag = self.ensure_tag(tag_name)
        _request("POST", f"/conversations/{conversation_id}/tags",
                 self.token, body={"tag_ids": [tag["id"]]})
        return tag

    def remove_tag(self, conversation_id: str, tag_name: str) -> bool:
        tag = self.find_tag_by_name(tag_name)
        if not tag:
            return False
        _request("DELETE", f"/conversations/{conversation_id}/tags",
                 self.token, body={"tag_ids": [tag["id"]]})
        return True

    def is_processed(self, conversation_id: str) -> bool:
        """Cost-control gate: True if conversation already bears edom-ai/processed."""
        return any(t.get("name") == PROCESSED_TAG for t in self.list_conversation_tags(conversation_id))

    def set_status(self, conversation_id: str, status: str) -> dict:
        data, _ = _request("PATCH", f"/conversations/{conversation_id}",
                           self.token, body={"status": status})
        return data

    def set_assignee(self, conversation_id: str, teammate_id: str) -> dict:
        data, _ = _request("PATCH", f"/conversations/{conversation_id}",
                           self.token, body={"assignee_id": teammate_id})
        return data

    def find_channel_for_conversation(self, conversation_id: str) -> Optional[str]:
        """Return the primary channel ID for a conversation (needed for draft creation)."""
        try:
            # Prefer the direct channel link if present
            conv = self.get_conversation(conversation_id)
            channel_url = ((conv.get("_links") or {}).get("related") or {}).get("channel", "") or ""
            match = re.search(r"/channels/([^/?#]+)", channel_url)
            if match:
                return match.group(1)
            # Fall back to calling the conversation's channels list endpoint
            data, _ = _request("GET", f"/conversations/{conversation_id}/channels", self.token)
            items = _result_items(data)
            if items:
                return items[0].get("id")
        except Exception as exc:
            logger.debug(f"find_channel_for_conversation({conversation_id}) failed: {exc}")
        return None

    def create_draft(self, conversation_id: Optional[str], *, channel_id: Optional[str] = None,
                     subject: str, body: str, to: list[str], mode: str = "private") -> dict:
        payload: dict[str, Any] = {"body": body, "subject": subject, "to": to, "mode": mode}
        if conversation_id:
            path = f"/conversations/{conversation_id}/drafts"
            if channel_id:
                payload["channel_id"] = channel_id
        else:
            if not channel_id:
                raise ValueError("Either conversation_id or channel_id is required for create_draft")
            path = f"/channels/{channel_id}/drafts"
        data, _ = _request("POST", path, self.token, body=payload)
        return data

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def extract_plain_text_body(message: dict) -> str:
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text
        html = message.get("body") or ""
        clean = re.sub(r"<style[\s\S]*?</style>", "", html, flags=re.I)
        clean = re.sub(r"<script[\s\S]*?</script>", "", clean, flags=re.I)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = (clean.replace("&nbsp;", " ").replace("&amp;", "&")
                 .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
        return re.sub(r"\s+", " ", clean).strip()

    @staticmethod
    def messages_to_transcript(messages: list[dict], *, max_chars: int = 12000) -> str:
        ordered = sorted(messages, key=lambda m: m.get("created_at") or 0)
        parts: list[str] = []
        total = 0
        for m in ordered:
            author = m.get("author") or {}
            sender = author.get("email") or author.get("handle") or "unknown"
            recipients = ", ".join(r.get("handle", "") for r in (m.get("to") or []))
            ts = m.get("created_at")
            date = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "unknown"
            body = FrontClient.extract_plain_text_body(m)
            direction = "inbound" if m.get("is_inbound") else "outbound"
            block = f"From: {sender}\nTo: {recipients}\nDate: {date}\nDirection: {direction}\n\n{body}\n\n---\n"
            if total + len(block) > max_chars:
                parts.append(f"[... {len(ordered) - len(parts)} earlier messages truncated ...]\n")
                break
            parts.append(block)
            total += len(block)
        return "".join(parts)
