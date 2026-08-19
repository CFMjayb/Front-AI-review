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
import logging
import os
import re

logger = logging.getLogger(__name__)

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


# ── Atlantic Union Positive Pay ───────────────────────────────────────────────

_ATLANTIC_UNION_DOMAINS = ("atlanticunionbank.com", "atlanticunion.com")

_POSITIVE_PAY_SUBJECT = re.compile(r"positive.?pay", re.I)

# Phrases in the email body that mean NO exceptions — safe to file silently.
_NO_EXCEPTION_PATTERNS = re.compile(
    r"no\s+(?:check\s+)?exceptions?\s+(?:today|found|to\s+review|reported|on\s+file)|"
    r"0\s+(?:check\s+)?exceptions?|"
    r"there\s+are\s+no\s+(?:items?|exceptions?)\s+(?:requiring|to)\s+(?:your\s+)?(?:review|decision|action)",
    re.I,
)


def _sender_domain(messages: list[dict]) -> str:
    inbound = [m for m in messages if m.get("is_inbound")]
    if not inbound:
        return ""
    latest = max(inbound, key=lambda m: m.get("created_at") or 0)
    email = _sender(latest)
    return email.split("@")[-1] if "@" in email else ""


def is_positive_pay(conv: dict, messages: list[dict]) -> tuple[bool, bool]:
    """Return (is_positive_pay_email, has_exceptions).

    is_positive_pay_email: True when this is an Atlantic Union Positive Pay notification.
    has_exceptions: True when the email reports items that need decisioning.
                    False means "no exceptions today" — file silently.
    """
    subject = conv.get("subject") or ""
    if not _POSITIVE_PAY_SUBJECT.search(subject):
        return False, False

    domain = _sender_domain(messages)
    if not any(domain == d or domain.endswith("." + d) for d in _ATLANTIC_UNION_DOMAINS):
        return False, False

    # It IS a Positive Pay email — now determine if it has exceptions
    body = ""
    inbound = [m for m in messages if m.get("is_inbound")]
    if inbound:
        from front_client import FrontClient
        latest = max(inbound, key=lambda m: m.get("created_at") or 0)
        body = FrontClient.extract_plain_text_body(latest) or ""

    has_exceptions = not bool(_NO_EXCEPTION_PATTERNS.search(body))
    return True, has_exceptions


def is_calendar_response(conv: dict) -> bool:
    """True for a calendar meeting-response notification (Accepted/Declined/Tentative).
    These are auto-generated and need no AI review or loop."""
    if os.environ.get("SPAM_PREFILTER", "true").lower() != "true":
        return False
    return bool(_CAL_RESPONSE.match(conv.get("subject") or ""))


def _sender(msg: dict) -> str:
    author = msg.get("author") or {}
    return (author.get("email") or author.get("handle") or "").strip().lower()


def sender_rule_skip(conv: dict, messages: list[dict]) -> tuple[bool, dict | None, str]:
    """(should_skip, rule, sender_email). True when a sender_rules lookup for the
    latest inbound sender says 'exclude' or 'fyi' — cheap enough to check before
    paying for a Claude review, unlike loop_from_thread's post-analysis version
    in cos/extract.py (which only downgrades a loop to FYI after the Claude call
    already happened)."""
    inbound = [m for m in messages if m.get("is_inbound")]
    if not inbound:
        return False, None, ""
    latest = max(inbound, key=lambda m: m.get("created_at") or 0)
    sender_email = _sender(latest)
    if not sender_email:
        return False, None, ""
    from cos import extract as cos_extract
    rule = cos_extract.sender_rule_action(sender_email)
    if not rule or rule.get("action") not in ("exclude", "fyi"):
        return False, None, sender_email

    # An optional subject_pattern narrows the rule to some of a sender's mail.
    # Needed for shared mailboxes: businessoffice@episcopalmaryland.org sends
    # both automated Beacon notifications (skip, no AI cost) and real mail from
    # real people (must still be reviewed). No pattern = the whole sender.
    pattern = (rule.get("subject_pattern") or "").strip()
    if pattern:
        try:
            if not re.search(pattern, conv.get("subject") or "", re.I):
                return False, None, sender_email
        except re.error:
            # A bad pattern must not silently swallow the sender's mail; fall
            # through to normal review and let the loop be created.
            logger.warning("sender rule for %s has an invalid subject_pattern %r",
                           sender_email, pattern)
            return False, None, sender_email
    return True, rule, sender_email


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
