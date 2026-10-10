"""
oauth_discovery.py -- EXPERIMENTAL per-user Entra ID OAuth discovery for MCP
clients (26-147 dev test, 2026-09-23). Only active when OAUTH_MODE is set;
with OAUTH_MODE unset main.py behaves exactly as before.

Env vars:
  OAUTH_MODE      "entra" -- protected-resource metadata points straight at
                             Entra ID (login.microsoftonline.com/{tenant}/v2.0)
                  "shim"  -- metadata points at THIS server, which serves an
                             RFC 8414 document whose authorize/token endpoints
                             are Entra ID's own (Entra never serves RFC 8414)
                  "proxy" -- like shim, but authorize/token go through this
                             server, which strips the RFC 8707 'resource'
                             parameter before forwarding to Entra ID
                             (Entra v2 rejects it: AADSTS9010010)
  OAUTH_RESOURCE  resource identifier advertised in the metadata
                  (default: api://{client_id})
  OAUTH_SCOPES    space-separated scopes advertised (default:
                  "api://{client_id}/.default offline_access")
  OAUTH_SECRET_PREFIX  Secret Manager prefix for this server's Entra app
                  (default "qbo" -> qbo-azure-tenant-id / qbo-azure-client-id;
                  "databank", "bill" on the sibling servers). Added 2026-09-23
                  for the databank/bill rollout; qbo behavior unchanged.
"""

import os
import urllib.parse

import requests
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

MODE = os.environ.get("OAUTH_MODE", "").strip().lower()
SECRET_PREFIX = os.environ.get("OAUTH_SECRET_PREFIX", "qbo").strip() or "qbo"

OPEN_PATHS = {
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/openid-configuration",
    "/oauth/authorize",
    "/oauth/token",
} if MODE else set()


def _cfg(get_secret):
    tenant = get_secret(f"{SECRET_PREFIX}-azure-tenant-id")
    client = get_secret(f"{SECRET_PREFIX}-azure-client-id")
    resource = os.environ.get("OAUTH_RESOURCE", "").strip() or f"api://{client}"
    scopes = (os.environ.get("OAUTH_SCOPES", "").strip()
              or f"api://{client}/.default offline_access").split()
    return tenant, client, resource, scopes


def _fix_scope(value, client):
    """Entra rejects 'api://{client}/...' when the app requests a token for
    itself (AADSTS90009) -- it must use the bare GUID form '{client}/...'."""
    return " ".join(sc.replace(f"api://{client}/", f"{client}/") for sc in value.split())


def prm_payload(get_secret, base_url):
    """RFC 9728 protected-resource metadata for the active mode."""
    tenant, _client, resource, scopes = _cfg(get_secret)
    auth_server = (f"https://login.microsoftonline.com/{tenant}/v2.0"
                   if MODE == "entra" else base_url)
    return {
        "resource": resource,
        "authorization_servers": [auth_server],
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base_url}/health",
    }


def register(mcp, get_secret, base_url, logger):
    if not MODE:
        return
    logger.info("OAUTH_MODE=%s -- experimental OAuth discovery routes active", MODE)

    async def _prm(request: Request):
        logger.info("OAUTH-TEST discovery hit: %s", request.url.path)
        return JSONResponse(prm_payload(get_secret, base_url),
                            headers={"Access-Control-Allow-Origin": "*"})

    mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])(_prm)

    async def _as_metadata(request: Request):
        logger.info("OAUTH-TEST discovery hit: %s", request.url.path)
        if MODE == "entra":
            return JSONResponse({"error": "not_found"}, status_code=404)
        tenant, _client, _res, scopes = _cfg(get_secret)
        ms = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        proxied = MODE == "proxy"
        return JSONResponse({
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize" if proxied else f"{ms}/authorize",
            "token_endpoint": f"{base_url}/oauth/token" if proxied else f"{ms}/token",
            "jwks_uri": f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
            "scopes_supported": scopes,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        }, headers={"Access-Control-Allow-Origin": "*"})

    for p in ("/.well-known/oauth-authorization-server",
              "/.well-known/oauth-authorization-server/mcp",
              "/.well-known/openid-configuration"):
        mcp.custom_route(p, methods=["GET"])(_as_metadata)

    async def _authorize(request: Request):
        tenant, client, *_ = _cfg(get_secret)
        params = [(k, _fix_scope(v, client) if k == "scope" else v)
                  for k, v in request.query_params.multi_items() if k != "resource"]
        logger.info("OAUTH-TEST authorize: scope=%s (resource dropped=%s)",
                    dict(params).get("scope"), "resource" in request.query_params)
        return RedirectResponse(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?"
            + urllib.parse.urlencode(params), status_code=302)

    async def _token(request: Request):
        tenant, client, *_ = _cfg(get_secret)
        form = [(k, _fix_scope(v, client) if k == "scope" else v)
                for k, v in (await request.form()).multi_items() if k != "resource"]
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if request.headers.get("authorization"):
            headers["Authorization"] = request.headers["authorization"]
        r = await run_in_threadpool(
            requests.post,
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=form, headers=headers, timeout=20)
        # Log only the outcome and Entra's error code, never token values.
        err = ""
        if r.status_code != 200:
            try:
                j = r.json()
                err = f"{j.get('error')}: {str(j.get('error_description', ''))[:200]}"
            except Exception:
                err = r.text[:200]
        logger.info("OAUTH-TEST token: grant=%s -> %s %s",
                    dict(form).get("grant_type"), r.status_code, err)
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))

    if MODE == "proxy":
        mcp.custom_route("/oauth/authorize", methods=["GET"])(_authorize)
        mcp.custom_route("/oauth/token", methods=["POST"])(_token)
