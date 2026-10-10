"""
bearer_auth.py -- per-user Microsoft (Entra ID) Bearer-token check for the
Front / Chief of Staff MCP server (26-147 rollout #5, 2026-10-10).

A signed-in person may use the MCP tools only if their token carries one of the
Azure AD group ids listed in the 'front-permissions' secret, e.g.
  {"groups": {"<group-object-id>": {"name": "QBO-Admins"}}}
Today that is QBO-Admins only (Jay's decision 2026-10-10); staff use Front's own
connector. The API key path stays for the triage workbooks (/api/cos/...).

Token validation is the same as the Email / QBO / BILL servers: signature via
Entra's JWKS, expiry, issuer (v1 or v2), audience api://{client_id} or the bare GUID.
The Entra app is QBO's (secrets front-mail-azure-tenant-id / front-mail-azure-client-id
hold its ids; OAUTH_SECRET_PREFIX=front-mail).
"""

import json
import logging
import time

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

PERMISSIONS_SECRET = "front-permissions"
_PERMISSIONS_TTL = 300  # seconds

_jwks_clients: dict = {}
_perm_cache: dict | None = None
_perm_cache_time: float = 0.0


def _jwks(tenant_id: str) -> PyJWKClient:
    if tenant_id not in _jwks_clients:
        _jwks_clients[tenant_id] = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
        )
    return _jwks_clients[tenant_id]


def validate_token(token: str, tenant_id: str, client_id: str) -> dict:
    """Return the decoded token payload, or raise jwt.InvalidTokenError."""
    key = _jwks(tenant_id).get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token, key.key, algorithms=["RS256"],
        options={"verify_exp": True, "verify_aud": False, "verify_iss": False},
    )
    if payload.get("iss", "") not in {
        f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        f"https://sts.windows.net/{tenant_id}/",
    }:
        raise jwt.InvalidIssuerError(f"Invalid issuer: {payload.get('iss')}")
    aud = payload.get("aud", "")
    if not ({aud} if isinstance(aud, str) else set(aud)) & {f"api://{client_id}", client_id}:
        raise jwt.InvalidAudienceError(f"Invalid audience: {aud}")
    return payload


def read_secret_fresh(name: str, get_secret_fn, cache: dict) -> str:
    """
    Read a secret bypassing the caller's per-instance, no-expiry secret cache.

    auth._get_secret() keeps every secret for the life of the Cloud Run instance, so without this an edit to
    'front-permissions' (a group added, or REMOVED) would not reach a running instance until it restarted
    (the bug found 2026-10-08 on the QBO / Databank / BILL / Email servers). _allowed_groups() only calls this
    when its own timer has expired, so Secret Manager is read at most about once per _PERMISSIONS_TTL per instance.

    If Secret Manager cannot be read, the last good copy is kept (and a warning logged) rather than
    failing every sign-in; only a first-ever read with nothing to fall back on raises.

    Args:
        name:          secret name, e.g. "front-permissions"
        get_secret_fn: the server's caching reader, i.e. auth._get_secret
        cache:         that reader's cache dict, i.e. auth._secret_cache
    """
    previous = cache.get(name)
    cache.pop(name, None)
    try:
        value = get_secret_fn(name)
    except Exception as exc:
        if previous is not None:
            cache[name] = previous
            logger.warning("Could not refresh secret '%s' (%s); keeping the last good copy.", name, exc)
            return previous
        raise
    state = "first read" if previous is None else ("changed" if value != previous else "unchanged")
    logger.info("Secret '%s' re-read from Secret Manager (%s).", name, state)
    return value


def _allowed_groups(get_secret) -> dict:
    global _perm_cache, _perm_cache_time
    if _perm_cache is None or time.time() - _perm_cache_time > _PERMISSIONS_TTL:
        raw = get_secret(PERMISSIONS_SECRET) or ""
        _perm_cache = json.loads(raw.lstrip("﻿") or '{"groups": {}}').get("groups", {})
        _perm_cache_time = time.time()
    return _perm_cache


def authorized_group(payload: dict, get_secret) -> str | None:
    """Name of the first permitted group the user belongs to, or None."""
    allowed = _allowed_groups(get_secret)
    for gid in payload.get("groups", []) or []:
        if gid in allowed:
            return allowed[gid].get("name", gid)
    return None


def display_name(payload: dict) -> str:
    return payload.get("name") or payload.get("preferred_username") or payload.get("upn") or payload.get("sub", "?")
