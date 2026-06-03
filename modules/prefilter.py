"""Cheap, AI-free pre-filter for unambiguous bulk / system mail.

Runs BEFORE the Claude analysis so we don't pay to AI-review marketing blasts and
bounce messages. **Precision over recall by design** — it only skips mail that is
safe to skip regardless of content:

  1. a dedicated *marketing-platform* sender domain (Mailchimp, Constant Contact,
     Campaign Monitor, Marketo, Pardot, Klaviyo, …), or
  2. a system / bounce sender (mailer-daemon, postmaster, …).

It deliberately does NOT use body footers like "unsubscribe": validation against
the AI's own labels showed those appear on legitimate transactional mail too
(EDOM payroll-deposit receipts, grant alerts, service notifications), so keying on
them wrongly skipped important email. General-purpose ESPs (SendGrid, Mailgun,
SparkPost, Mailjet) are also excluded because orgs send transactional mail through
them. Anything ambiguous still gets the full AI review (which then labels real
spam at ~$0.02). Never fires on a thread Jay has already replied to.

A match is tagged AI/spam + AI/processed and skipped. Disable with SPAM_PREFILTER=false.
"""
import os
import re

# Dedicated MARKETING email-platform sending domains. These send marketing/
# newsletter blasts (not transactional mail), so they're safe to skip without
# reading content. General-purpose ESPs are intentionally NOT here.
_MARKETING_ESP_DOMAINS = (
    "mcsv.net", "mcdlv.net", "rsgsv.net", "list-manage.com",   # Mailchimp
    "ccsend.com", "constantcontact.com",                         # Constant Contact
    "createsend.com", "cmail19.com", "cmail20.com",             # Campaign Monitor
    "mktomail.com",                                              # Marketo
    "pardot.com",                                                # Pardot
    "klaviyomail.com",                                           # Klaviyo
    "icontact.com",                                              # iContact
    "benchmarkemail.com",                                        # Benchmark
    "sendinblue.com", "sib.email",                              # Brevo (marketing)
    "hubspotemail.net", "hs-send.com",                          # HubSpot (marketing)
    "exct.net",                                                  # Salesforce Marketing Cloud
)

# System / bounce senders — never need an AI review.
_SYSTEM_LOCALPART = re.compile(
    r"^(?:mailer-daemon|postmaster|bounce[sd]?|.*-bounces?)$", re.I
)

# Calendar meeting-response auto-notifications (Outlook/O365 subject format).
# "Accepted: <meeting>", "Declined: …", "Tentative: …" — informational, never a loop.
_CAL_RESPONSE = re.compile(
    r"^\s*(?:accepted|declined|tentative|tentatively accepted|"
    r"new time proposed|declined with comments)\s*:",
    re.I,
)


def is_calendar_response(conv: dict) -> bool:
    """True for a calendar meeting-response notification (Accepted/Declined/Tentative).
    These are auto-generated and need no AI review or loop."""
    if os.environ.get("SPAM_PREFILTER", "true").lower() != "true":
        return False
    return bool(_CAL_RESPONSE.match(conv.get("subject") or ""))


def _sender(msg: dict) -> str:
    author = msg.get("author") or {}
    return (author.get("email") or author.get("handle") or "").strip().lower()


def looks_like_bulk(conv: dict, messages: list[dict]) -> tuple[bool, str | None]:
    """Return (is_bulk, reason). Only obvious marketing-platform / system mail."""
    if os.environ.get("SPAM_PREFILTER", "true").lower() != "true":
        return (False, None)

    inbound = [m for m in messages if m.get("is_inbound")]
    outbound = [m for m in messages if not m.get("is_inbound")]

    # If Jay has already replied, this is a real thread — never pre-filter it.
    if outbound or not inbound:
        return (False, None)

    latest = max(inbound, key=lambda m: m.get("created_at") or 0)
    sender = _sender(latest)
    localpart, _, domain = sender.partition("@")

    if domain and any(domain == d or domain.endswith("." + d) for d in _MARKETING_ESP_DOMAINS):
        return (True, f"marketing-platform sender domain ({domain})")

    if _SYSTEM_LOCALPART.match(localpart):
        return (True, f"system/bounce sender ({sender or 'unknown'})")

    return (False, None)
