"""Outbound SMS notifier for time-sensitive CoS alerts.

Backed by Twilio. Requires three environment variables (or GCP secrets):
  TWILIO_ACCOUNT_SID   — Twilio account SID
  TWILIO_AUTH_TOKEN    — Twilio auth token
  TWILIO_FROM_NUMBER   — the Twilio phone number to send from (E.164, e.g. +12025551234)
  SMS_NOTIFY_TO        — recipient phone number (E.164, e.g. +12405551234)

If any variable is missing, send_sms() logs a warning and returns False so callers
don't need to guard against it. This keeps production pipelines working even before
Twilio is wired up — the loop is still created, just no text is sent.

To add to GCP secrets:
  echo -n "ACxxxxxxx" | gcloud secrets versions add twilio-account-sid --data-file=- --project=cfm-front-mail
  (etc. for twilio-auth-token, twilio-from-number, sms-notify-to)
"""
import logging
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import base64

logger = logging.getLogger(__name__)


def _get(name: str) -> str:
    """Read from env or GCP Secret Manager (reuses auth.py pattern)."""
    val = os.environ.get(name, "")
    if val:
        return val
    # Try Secret Manager if configured
    gcp_project = os.environ.get("GCP_PROJECT", "")
    use_sm = os.environ.get("USE_SECRET_MANAGER", "false").lower() == "true"
    if use_sm and gcp_project:
        secret_name = name.lower().replace("_", "-")
        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            resource = client.secret_version_path(gcp_project, secret_name, "latest")
            response = client.access_secret_version(request={"name": resource})
            return response.payload.data.decode("utf-8-sig").strip()
        except Exception:
            pass
    return ""


def send_sms(message: str) -> bool:
    """Send an SMS via Twilio. Returns True on success, False if not configured or failed."""
    account_sid  = _get("TWILIO_ACCOUNT_SID")
    auth_token   = _get("TWILIO_AUTH_TOKEN")
    from_number  = _get("TWILIO_FROM_NUMBER")
    to_number    = _get("SMS_NOTIFY_TO")

    if not all([account_sid, auth_token, from_number, to_number]):
        logger.warning("SMS notification skipped — Twilio credentials not configured "
                       "(set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, SMS_NOTIFY_TO)")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    body = urlencode({"From": from_number, "To": to_number, "Body": message}).encode("utf-8")
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req = Request(url, data=body, headers={
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, method="POST")

    try:
        with urlopen(req, timeout=15) as resp:
            logger.info(f"SMS sent to {to_number}: HTTP {resp.status}")
            return True
    except HTTPError as exc:
        logger.error(f"SMS failed: HTTP {exc.code} — {exc.read().decode('utf-8', errors='replace')}")
        return False
    except Exception as exc:
        logger.error(f"SMS failed: {exc}")
        return False
