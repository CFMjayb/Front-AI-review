"""Secret resolution: env var first, GCP Secret Manager fallback.

Mirrors front-mail-organizer/auth.py.
"""
import os
import logging
from typing import Optional

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

logger = logging.getLogger(__name__)

USE_SECRET_MANAGER = os.environ.get("USE_SECRET_MANAGER", "false").lower() == "true"
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")

_secret_cache: dict[str, str] = {}


def _sm_client():
    from google.cloud import secretmanager  # type: ignore
    return secretmanager.SecretManagerServiceClient()


def _get_secret(name: str) -> str:
    """Read a secret. Production: GCP Secret Manager. Dev: env var (uppercase, hyphens → underscores)."""
    if name in _secret_cache:
        return _secret_cache[name]

    if USE_SECRET_MANAGER and GCP_PROJECT:
        try:
            client = _sm_client()
            resource_name = client.secret_version_path(GCP_PROJECT, name, "latest")
            response = client.access_secret_version(request={"name": resource_name})
            value = response.payload.data.decode("utf-8-sig").strip()
            _secret_cache[name] = value
            return value
        except Exception as exc:
            logger.warning(f"Secret Manager read failed for {name}: {exc}. Falling back to env.")

    env_name = name.upper().replace("-", "_")
    value = os.environ.get(env_name, "")
    _secret_cache[name] = value
    return value


def get_front_api_token() -> str:
    token = _get_secret("front-api-token") or os.environ.get("FRONT_API_TOKEN", "")
    if not token:
        raise RuntimeError("FRONT_API_TOKEN not configured. Set env var or GCP secret 'front-api-token'.")
    return token


def get_anthropic_api_key() -> str:
    key = _get_secret("anthropic-api-key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured. Set env var or GCP secret 'anthropic-api-key'.")
    return key
