"""EDOM Chief-of-Staff MCP server — exposes the open-loop ledger as tools.

The conversational surface: from Claude Code or chat, Jay asks "what do I owe
people this week?" and the agent calls cos_list_loops. Ingestion writes loops
here via cos_upsert_loop.

HTTP mode (Cloud Run, default): python cos_mcp_server.py
Stdio mode (local dev):         TRANSPORT=stdio python cos_mcp_server.py

Mirrors mcp_server.py: same FastMCP setup and X-API-Key middleware.
"""
import logging
import os

from dotenv import load_dotenv
load_dotenv(override=True)

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from cos import ledger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

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
    "edom-cos",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse({"service": "edom-cos", "status": "running"})


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "edom-cos"})


# ── Loop tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def cos_list_loops(direction: str = "", channel: str = "", status: str = "",
                   overdue_only: bool = False, include_resolved: bool = False,
                   mailbox: str = "") -> list:
    """List open loops. direction: i_owe|owed_to_me. channel: front|outlook|teams|zoom.
    status: open|waiting|snoozed|done|dropped. Resolved loops hidden unless requested.
    mailbox: cfm|edom|other — which mailbox the loop came from (see cos/mailboxes.py);
    omit for all mailboxes."""
    return ledger.list_loops(direction=direction, channel=channel, status=status,
                             overdue_only=overdue_only, include_resolved=include_resolved,
                             mailbox=mailbox)


@mcp.tool()
def cos_get_loop(loop_id: str) -> dict:
    """Get a single loop by id."""
    return ledger.get_loop(loop_id) or {}


@mcp.tool()
def cos_upsert_loop(direction: str, counterparty: str, summary: str, channel: str,
                    source_ref: str, source_link: str = "", counterparty_email: str = "",
                    category: str = "", importance: int = 3, confidence: float = 0.0,
                    due_at: str = "", status: str = "", last_activity: str = "") -> dict:
    """Create or update a loop (idempotent on channel+source_ref+direction).
    direction: 'i_owe' (they wait on Jay) | 'owed_to_me' (Jay waits on them).
    A manually-set status (done/dropped/snoozed) is never overwritten by upsert."""
    return ledger.upsert_loop(
        direction=direction, counterparty=counterparty, summary=summary, channel=channel,
        source_ref=source_ref, source_link=source_link, counterparty_email=counterparty_email,
        category=category, importance=importance, confidence=confidence, due_at=due_at,
        status=status, last_activity=last_activity)


@mcp.tool()
def cos_resolve_loop(num: int = 0, status: str = "done", loop_id: str = "",
                     reason: str = "") -> dict:
    """Resolve a loop by its catalog number (#num, as shown in the briefing).
    status: done | dropped | open | waiting | snoozed. An optional short reason
    (e.g. 'noise', 'handled in bank portal') is logged as learning signal.
    (loop_id is an alternative key.)"""
    if loop_id:
        return ledger.resolve_loop(loop_id, status, reason=reason) or {}
    result = ledger.resolve_by_num(num, status, reason=reason)
    return result or {"error": f"no loop #{num}"}


@mcp.tool()
def cos_snooze_loop(num: int = 0, until: str = "", loop_id: str = "",
                    reason: str = "") -> dict:
    """Snooze a loop by its catalog number (#num) until an ISO date/timestamp
    (hidden from the briefing until then). Optional reason is logged for learning.
    (loop_id is an alternative key.)"""
    if loop_id:
        return ledger.snooze_loop(loop_id, until, reason=reason) or {}
    result = ledger.snooze_by_num(num, until, reason=reason)
    return result or {"error": f"no loop #{num}"}


@mcp.tool()
def cos_list_feedback(action: str = "", since: str = "", limit: int = 200) -> list:
    """Read the resolution feedback log (how loops were resolved over time).
    action: done | dropped | snoozed. The substrate for learning what Jay treats
    as noise vs. important."""
    return ledger.list_feedback(action=action, since=since, limit=limit)


@mcp.tool()
def cos_stats() -> dict:
    """Summary counts: total, open by direction, by status, overdue."""
    return ledger.stats()


# ── Memory tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def cos_remember(key: str, value: str) -> dict:
    """Store a piece of standing memory (e.g. 'priorities', 'voice')."""
    return ledger.remember(key, value)


@mcp.tool()
def cos_get_memory(key: str = "") -> dict:
    """Read standing memory. Pass a key for one item, or omit for all."""
    return ledger.get_memory(key)


@mcp.tool()
def cos_people_upsert(key: str, name: str = "", role: str = "", importance: int = 3,
                      notes: str = "") -> dict:
    """Add or update a known counterparty. key = normalized email. importance 1-5."""
    return ledger.people_upsert(key=key, name=name, role=role, importance=importance,
                                notes=notes)


@mcp.tool()
def cos_list_people() -> list:
    """List known counterparties, most important first."""
    return ledger.list_people()


# ── App factory ───────────────────────────────────────────────────────────────

def create_app():
    starlette_app = mcp.streamable_http_app()
    starlette_app.add_middleware(ApiKeyMiddleware)
    return starlette_app


app = create_app()


if __name__ == "__main__":
    ledger.init_db()
    transport = os.environ.get("TRANSPORT", "http")
    if transport == "stdio":
        mcp.run()
    else:
        import uvicorn
        port = int(os.environ.get("PORT", 8081))
        logger.info("Starting edom-cos MCP server on port %d", port)
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
