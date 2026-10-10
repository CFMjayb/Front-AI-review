"""
mcp_auth.py -- request authentication for the Front / Chief of Staff server (26-147 rollout #5, 2026-10-10).

Two ways in, on two different kinds of path:

  /mcp, /mcp/...   the Claude connector. A person's Microsoft sign-in token (Bearer) is checked against the
                   'front-permissions' group list. The shared API key is ALSO accepted here only while
                   MCP_ALLOW_API_KEY is true (phase A of the rollout); setting it to false (phase B) leaves
                   Microsoft sign-in as the only way into the tools.
  everything else  /api/cos/... (the triage workbooks, which have the key built in) stays on the API key.
                   A Bearer token is not accepted there.

Open with no credentials: "/", "/health" and the sign-in discovery pages (so a connector can find the sign-in).

Kept out of mcp_server.py so the rules can be tested without importing the whole server.
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import bearer_auth
import oauth_discovery

logger = logging.getLogger(__name__)


def is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, get_secret, secret_cache, api_key, cloud_run_url,
                 allow_api_key_on_mcp=True, oauth_on=None, open_paths=None):
        super().__init__(app)
        self._get_secret = get_secret
        self._secret_cache = secret_cache
        self._api_key = api_key
        self._url = cloud_run_url.rstrip("/")
        self._allow_key_on_mcp = allow_api_key_on_mcp
        self._oauth_on = bool(oauth_discovery.MODE) if oauth_on is None else oauth_on
        self._open = set(open_paths) if open_paths is not None else {"/", "/health"}

    def _www_auth(self) -> dict:
        """Points MCP clients at the sign-in metadata; empty when sign-in is off."""
        if not self._oauth_on:
            return {}
        return {"WWW-Authenticate": (
            f'Bearer realm="front-ai-review", '
            f'resource_metadata="{self._url}/.well-known/oauth-protected-resource"')}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self._open or request.method == "OPTIONS":
            return await call_next(request)

        on_mcp = is_mcp_path(path)

        # Layer 1 -- a person's Microsoft (Entra ID) sign-in token; the connector path only
        auth_header = request.headers.get("Authorization", "")
        if on_mcp and self._oauth_on and auth_header.startswith("Bearer "):
            try:
                tenant_id, client_id, *_ = oauth_discovery._cfg(self._get_secret)
                payload = bearer_auth.validate_token(auth_header[7:].strip(), tenant_id, client_id)
            except Exception as exc:
                logger.warning("Azure AD token validation failed: %s", exc)
                return JSONResponse({"error": "Invalid Bearer token", "detail": str(exc)},
                                    status_code=401, headers=self._www_auth())
            who = bearer_auth.display_name(payload)
            group = bearer_auth.authorized_group(
                payload, lambda n: bearer_auth.read_secret_fresh(n, self._get_secret, self._secret_cache))
            if not group:
                logger.warning("Azure AD user '%s' signed in but is not in a front-permissions group", who)
                return JSONResponse(
                    {"error": "Forbidden",
                     "detail": "Your account is not permitted to use the Front tools."},
                    status_code=403)
            logger.info("Authenticated via Azure AD: %s (%s)", who, group)
            return await call_next(request)

        # Layer 2 -- the shared API key: the workbook paths always, the connector path only in phase A
        if on_mcp and not self._allow_key_on_mcp:
            return JSONResponse(
                {"error": "Authentication required",
                 "detail": "Sign in with Microsoft. API keys are not accepted on this path."},
                status_code=401, headers=self._www_auth())

        api_key = (
            request.headers.get("X-API-Key", "")
            or request.query_params.get("api_key", "")
        )
        if not api_key:
            return JSONResponse(
                {"error": "Authentication required",
                 "detail": "Sign in with Microsoft, or provide an X-API-Key header."
                           if on_mcp else "Provide an X-API-Key header."},
                status_code=401, headers=self._www_auth() if on_mcp else {})
        if not self._api_key:
            logger.error("MCP_API_KEY not configured")
            return JSONResponse({"error": "Server misconfigured"}, status_code=500)
        if api_key != self._api_key:
            logger.warning("Rejected invalid API key from %s",
                           request.client.host if request.client else "unknown")
            return JSONResponse({"error": "Unauthorized"}, status_code=403)
        return await call_next(request)
