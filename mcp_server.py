"""EDOM Front MCP server — exposes Front API as tools callable from Claude Code.

HTTP mode (Cloud Run, default): python mcp_server.py
Stdio mode (local dev):         TRANSPORT=stdio python mcp_server.py
"""
import asyncio
import datetime
import os
import re
import logging

from dotenv import load_dotenv
load_dotenv(override=True)

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth import get_front_api_token
from front_client import FrontClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Injected by Cloud Run --set-secrets or set in .env for dev
_MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

_OPEN_PATHS = {"/", "/health"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _OPEN_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        api_key = (
            request.headers.get("X-API-Key", "")
            or request.query_params.get("api_key", "")
        )
        if not api_key:
            return JSONResponse({"error": "Missing X-API-Key header"}, status_code=401)
        if not _MCP_API_KEY:
            logger.error("MCP_API_KEY not configured")
            return JSONResponse({"error": "Server misconfigured"}, status_code=500)
        if api_key != _MCP_API_KEY:
            logger.warning("Rejected invalid API key from %s",
                           request.client.host if request.client else "unknown")
            return JSONResponse({"error": "Unauthorized"}, status_code=403)
        return await call_next(request)


mcp = FastMCP(
    "edom-front",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _front() -> FrontClient:
    return FrontClient(get_front_api_token())


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse({"service": "edom-front", "status": "running"})


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "edom-front"})


# ── Read tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def front_list_inboxes() -> list:
    """List all Front inboxes accessible to the EDOM token."""
    return _front().list_inboxes()


@mcp.tool()
def front_list_channels() -> list:
    """List all Front channels (email addresses)."""
    return _front().list_channels()


@mcp.tool()
def front_list_teammates(email: str = "") -> list:
    """List Front teammates. Pass email to filter by address."""
    return _front().list_teammates(email=email or None)


@mcp.tool()
def front_list_inbox_conversations(inbox_id: str, status: str = "open",
                                   since_ms: int = 0, limit: int = 50) -> list:
    """List conversations in a Front inbox. status: open|archived|deleted|spam."""
    return _front().list_inbox_conversations(
        inbox_id, status=status or None,
        since_ms=since_ms or None, limit=limit,
    )


@mcp.tool()
def front_list_assigned_conversations(teammate_id: str, since_ms: int = 0) -> list:
    """List conversations assigned to a teammate."""
    return _front().list_assigned_conversations(teammate_id, since_ms=since_ms or None)


@mcp.tool()
def front_get_conversation(conversation_id: str) -> dict:
    """Get a single conversation's metadata."""
    return _front().get_conversation(conversation_id)


@mcp.tool()
def front_get_conversation_messages(conversation_id: str) -> list:
    """Get all messages in a conversation."""
    return _front().get_conversation_messages(conversation_id)


@mcp.tool()
def front_get_conversation_comments(conversation_id: str) -> list:
    """Get all internal comments on a conversation."""
    return _front().get_conversation_comments(conversation_id)


@mcp.tool()
def front_list_conversation_tags(conversation_id: str) -> list:
    """List tags currently applied to a conversation."""
    return _front().list_conversation_tags(conversation_id)


@mcp.tool()
def front_search_conversations(query: str) -> list:
    """Search conversations by Front query string."""
    return _front().search_conversations(query)


@mcp.tool()
def front_list_tags() -> list:
    """List all tags in the Front workspace."""
    return _front().list_tags()


# ── Write tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def front_add_comment(conversation_id: str, body: str) -> dict:
    """Add an internal comment to a conversation."""
    return _front().add_comment(conversation_id, body)


@mcp.tool()
def front_add_tag(conversation_id: str, tag_name: str) -> dict:
    """Add a tag to a conversation by name (creates the tag if it does not exist)."""
    return _front().add_tag(conversation_id, tag_name)


@mcp.tool()
def front_remove_tag(conversation_id: str, tag_name: str) -> bool:
    """Remove a tag from a conversation by name."""
    return _front().remove_tag(conversation_id, tag_name)


@mcp.tool()
def front_set_status(conversation_id: str, status: str) -> dict:
    """Set conversation status: open | archived | deleted | spam."""
    return _front().set_status(conversation_id, status)


@mcp.tool()
def front_set_assignee(conversation_id: str, teammate_id: str) -> dict:
    """Set a conversation's assignee to a teammate ID."""
    return _front().set_assignee(conversation_id, teammate_id)


@mcp.tool()
def front_create_draft(conversation_id: str, subject: str, body: str,
                       to: list[str]) -> dict:
    """Create a draft reply. Saved for human review — never auto-sent."""
    return _front().create_draft(conversation_id, subject=subject, body=body, to=to)


# ── CoS Triage REST API ───────────────────────────────────────────────────────

def _cos_ledger():
    """Lazy import so ledger reads LEDGER_BACKEND after env is fully loaded."""
    from cos import ledger as _l
    return _l


def _age_days(first_seen):
    if not first_seen:
        return ""
    try:
        d = datetime.date.fromisoformat(str(first_seen)[:10])
        return (datetime.date.today() - d).days
    except Exception:
        return ""


def _date_str(s):
    return str(s)[:10] if s else ""


def _tsv_safe(v):
    if v is None:
        return ""
    return str(v).replace("\t", " ").replace("\n", " ").replace("\r", "")


_URGENCY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _loop_sort_key(loop):
    if loop.get("fyi"):
        return (10, 0, loop.get("first_seen") or "")
    urg = _URGENCY_ORDER.get((loop.get("urgency") or "normal").lower(), 4)
    dir_order = 0 if loop.get("direction") == "i_owe" else 1
    return (urg, dir_order, loop.get("first_seen") or "9999")


def _loop_to_tsv_row(loop, row_type):
    urgency   = (loop.get("urgency") or "normal").lower()
    direction = loop.get("direction", "owed_to_me")
    if row_type == "deferred":
        dir_label = "Deferred"
    elif loop.get("fyi"):
        dir_label = "FYI"
    elif direction == "i_owe":
        dir_label = "On You"
    else:
        dir_label = "Waiting"
    raw_sent = (loop.get("sentiment") or "").lower()
    sent_display = raw_sent if raw_sent in ("concerned", "frustrated", "angry") else ""
    return "\t".join([
        _tsv_safe(loop.get("id")),
        _tsv_safe(loop.get("num")),
        row_type,
        urgency,
        direction,
        dir_label,
        _tsv_safe(loop.get("action_type")),
        _tsv_safe(loop.get("counterparty")),
        _tsv_safe(loop.get("summary")),
        _tsv_safe(loop.get("category")),
        str(_age_days(loop.get("first_seen"))),
        _date_str(loop.get("due_at")),
        _date_str(loop.get("source_date")),
        sent_display,
        _tsv_safe(loop.get("source_link")),
    ])


def _parse_snooze_until_cos(value):
    v = value.strip().lower()
    now = datetime.datetime.now(datetime.timezone.utc)
    m = re.match(r"snooze\s+(\d{4}-\d{2}-\d{2})$", v)
    if m:
        return m.group(1) + "T00:00:00Z"
    m = re.match(r"snooze\s+(\d+)([dwm])$", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = (datetime.timedelta(days=n) if unit == "d" else
                 datetime.timedelta(weeks=n) if unit == "w" else
                 datetime.timedelta(days=n * 30))
        return (now + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _front_archive(loop_rec):
    if not loop_rec or loop_rec.get("channel") != "front":
        return False
    src = loop_rec.get("source_ref")
    if not src:
        return False
    try:
        conv = _front().get_conversation(src)
        if conv.get("status") not in {"open", "assigned", "unassigned"}:
            return True
        _front().set_status(src, "archived")
        return True
    except Exception as exc:
        logger.warning("Could not archive Front conversation %s: %s", src, exc)
        return False


@mcp.custom_route("/api/cos/loops", methods=["GET"])
async def cos_get_loops(request: Request):
    try:
        ldr = _cos_ledger()
        active   = sorted(ldr.list_loops(), key=_loop_sort_key)
        deferred = ldr.list_loops(deferred_only=True)
        header = ("id\tnum\trow_type\turgency\tdirection\tdir_label\t"
                  "action_type\tcounterparty\tsummary\tcategory\t"
                  "age_days\tdue_at\tsource_date\tsentiment_display\tsource_link")
        rows = [header]
        for loop in active:
            rows.append(_loop_to_tsv_row(loop, "active"))
        if deferred:
            rows.append("\t\tdivider\t\t\t\t\t\t\t\t\t\t\t\t")
            for loop in deferred:
                rows.append(_loop_to_tsv_row(loop, "deferred"))
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("\n".join(rows))
    except Exception as exc:
        logger.exception("GET /api/cos/loops failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/cos/triage", methods=["POST"])
async def cos_triage_action(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    loop_id_val = str(body.get("id") or "").strip()
    action      = str(body.get("action") or "").strip().lower()
    notes       = str(body.get("notes") or "").strip()

    if not loop_id_val:
        return JSONResponse({"error": "id is required"}, status_code=400)

    try:
        ldr = _cos_ledger()
        loop_rec = ldr.get_loop(loop_id_val)
        if not loop_rec:
            return JSONResponse({"error": "Loop not found"}, status_code=404)

        if notes:
            existing = loop_rec.get("notes") or ""
            combined = (existing + "\n" + notes).strip()
            ldr.patch_loop(loop_id_val, notes=combined)

        if not action:
            return JSONResponse({"status": "ok", "action": "notes_saved", "id": loop_id_val})

        if action == "done":
            ldr.resolve_loop(loop_id_val, "done")
            if _front_archive(loop_rec):
                ldr.patch_loop(loop_id_val, front_archived=True)

        elif action == "drop":
            ldr.resolve_loop(loop_id_val, "dropped")
            if _front_archive(loop_rec):
                ldr.patch_loop(loop_id_val, front_archived=True)

        elif action == "exclude":
            ldr.patch_loop(loop_id_val, category="junk")
            ldr.resolve_loop(loop_id_val, "dropped", reason="excluded:junk")
            if _front_archive(loop_rec):
                ldr.patch_loop(loop_id_val, front_archived=True)

        elif action == "subscribe":
            if loop_rec.get("channel") == "front" and loop_rec.get("source_ref"):
                try:
                    _front().add_tag(loop_rec["source_ref"], "cos/reading-list")
                except Exception as exc:
                    logger.warning("Could not tag in Front: %s", exc)
            ldr.resolve_loop(loop_id_val, "dropped", reason="subscribed:reading-list")

        elif action == "fyi":
            ldr.patch_loop(loop_id_val, fyi=True, deferred=False)

        elif action == "defer":
            ldr.patch_loop(loop_id_val, deferred=True)

        elif action.startswith("snooze"):
            until = _parse_snooze_until_cos(action)
            if not until:
                return JSONResponse({"error": f"Cannot parse snooze date from '{action}'"}, status_code=400)
            ldr.snooze_loop(loop_id_val, until)

        else:
            return JSONResponse({"error": f"Unknown action '{action}'"}, status_code=400)

        return JSONResponse({"status": "ok", "action": action, "id": loop_id_val})

    except Exception as exc:
        logger.exception("POST /api/cos/triage failed id=%s action=%s", loop_id_val, action)
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/cos/sender-rules", methods=["GET"])
async def cos_get_sender_rules(request: Request):
    try:
        rules = _cos_ledger().list_sender_rules()
        header = "email\taction\tcategory\tdirection\timportance\tsubject_pattern\tnotes"
        rows = [header]
        for r in rules:
            rows.append("\t".join([
                _tsv_safe(r.get("email")),
                _tsv_safe(r.get("action")),
                _tsv_safe(r.get("category")),
                _tsv_safe(r.get("direction")),
                _tsv_safe(r.get("importance")),
                _tsv_safe(r.get("subject_pattern")),
                _tsv_safe(r.get("notes")),
            ]))
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("\n".join(rows))
    except Exception as exc:
        logger.exception("GET /api/cos/sender-rules failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/cos/sender-rules-save", methods=["POST"])
async def cos_save_sender_rules(request: Request):
    try:
        rules = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(rules, list):
        return JSONResponse({"error": "Expected JSON array"}, status_code=400)
    try:
        ldr = _cos_ledger()
        upserted = deleted = 0
        for rule in rules:
            email = str(rule.get("email") or "").strip().lower()
            if not email:
                continue
            if str(rule.get("_delete") or "").strip().lower() == "yes":
                if ldr.delete_sender_rule(email):
                    deleted += 1
                continue
            action = str(rule.get("action") or "").strip().lower()
            if not action:
                continue
            imp_raw = rule.get("importance")
            try:
                imp = int(float(str(imp_raw))) if imp_raw else 0
            except (ValueError, TypeError):
                imp = 0
            ldr.upsert_sender_rule(
                email=email, action=action,
                category=str(rule.get("category") or ""),
                direction=str(rule.get("direction") or ""),
                importance=imp,
                subject_pattern=str(rule.get("subject_pattern") or ""),
                notes=str(rule.get("notes") or ""),
            )
            upserted += 1
        return JSONResponse({"status": "ok", "upserted": upserted, "deleted": deleted})
    except Exception as exc:
        logger.exception("POST /api/cos/sender-rules-save failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/cos/guidance", methods=["GET"])
async def cos_get_guidance(request: Request):
    try:
        items = _cos_ledger().list_guidance()
        header = "key\tscope\tbody\tactive"
        rows = [header]
        for g in items:
            rows.append("\t".join([
                _tsv_safe(g.get("key")),
                _tsv_safe(g.get("scope")),
                _tsv_safe(g.get("body")),
                "yes" if g.get("active") else "no",
            ]))
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("\n".join(rows))
    except Exception as exc:
        logger.exception("GET /api/cos/guidance failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/cos/guidance-save", methods=["POST"])
async def cos_save_guidance(request: Request):
    try:
        items = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(items, list):
        return JSONResponse({"error": "Expected JSON array"}, status_code=400)
    try:
        ldr = _cos_ledger()
        upserted = deleted = 0
        for g in items:
            key = str(g.get("key") or "").strip().lower()
            if not key:
                continue
            if str(g.get("_delete") or "").strip().lower() == "yes":
                if ldr.delete_guidance(key):
                    deleted += 1
                continue
            body = str(g.get("body") or "").strip()
            if not body:
                continue
            scope = str(g.get("scope") or "all").strip()
            active_raw = str(g.get("active") or "yes").strip().lower()
            active = active_raw not in ("no", "false", "0")
            ldr.upsert_guidance(key=key, body=body, scope=scope or "all", active=active)
            upserted += 1
        return JSONResponse({"status": "ok", "upserted": upserted, "deleted": deleted})
    except Exception as exc:
        logger.exception("POST /api/cos/guidance-save failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/cos/briefing", methods=["POST"])
async def cos_send_briefing(request: Request):
    try:
        from cos.briefing import run_briefing
        result = await asyncio.get_event_loop().run_in_executor(None, run_briefing)
        return JSONResponse({
            "status": "ok",
            "subject": result.get("subject", ""),
            "transport": result.get("transport", ""),
            "counts": result.get("counts", {}),
        })
    except Exception as exc:
        logger.exception("POST /api/cos/briefing failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app():
    starlette_app = mcp.streamable_http_app()
    starlette_app.add_middleware(ApiKeyMiddleware)
    return starlette_app


app = create_app()


if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "http")
    if transport == "stdio":
        mcp.run()
    else:
        import uvicorn
        port = int(os.environ.get("PORT", 8080))
        logger.info("Starting edom-front MCP server on port %d", port)
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
