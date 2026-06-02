"""Cheap, AI-free pre-filter for obvious bulk / marketing / system mail.

Runs BEFORE the Claude analysis so we don't pay to AI-review newsletters,
no-reply blasts, and bounce messages. Conservative by design: it only fires on
strong, unambiguous bulk signals —

  1. a known bulk-ESP sender domain (Mailchimp, Constant Contact, SendGrid, …),
  2. a system / bounce sender (mailer-daemon, postmaster, …), or
  3. a classic bulk-mail footer in the body (unsubscribe / manage preferences / …),

and NEVER on a thread Jay has already replied to. Everything ambiguous still
goes to the full AI review. A match is tagged AI/spam + AI/processed and skipped,
saving the per-conversation Claude cost.

Disable with SPAM_PREFILTER=false.
"""
import os
import re

# Classic bulk-mail footer phrases. A genuine 1:1 human email essentially never
# contains these; marketing / newsletters almost always do.
_BULK_BODY = re.compile(
    r"unsubscribe"
    r"|view (?:this|it)(?: email)? in your browser"
    r"|manage (?:your )?(?:email )?preferences"
    r"|update your (?:email )?preferences"
    r"|you(?:'re| are) receiving this (?:email|message)"
    r"|no longer wish to receive"
    r"|opt[- ]?out"
    r"|add us to your address book",
    re.I,
)

# Known bulk email-service-provider sending domains.
_ESP_DOMAINS = (
    "mailchimp.com", "mcsv.net", "mcdlv.net", "rsgsv.net",
    "sendgrid.net", "sendgrid.com", "sparkpostmail.com",
    "constantcontact.com", "ccsend.com",
    "createsend.com", "cmail19.com", "cmail20.com",
    "hubspotemail.net", "hs-send.com",
    "mailgun.org", "sendinblue.com", "sib.email", "list-manage.com",
    "exct.net", "mailjet.com",
    "marketo.com", "mktomail.com", "pardot.com",
    "klaviyomail.com", "icontact.com", "benchmarkemail.com",
)

# System / bounce senders — never need an AI review.
_SYSTEM_LOCALPART = re.compile(
    r"^(?:mailer-daemon|postmaster|bounce[sd]?|.*-bounces?)$", re.I
)


def _sender(msg: dict) -> str:
    author = msg.get("author") or {}
    return (author.get("email") or author.get("handle") or "").strip().lower()


def looks_like_bulk(conv: dict, messages: list[dict]) -> tuple[bool, str | None]:
    """Return (is_bulk, reason). Conservative — only obvious bulk / system mail."""
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

    if domain and any(domain == d or domain.endswith("." + d) for d in _ESP_DOMAINS):
        return (True, f"bulk ESP sender domain ({domain})")

    if _SYSTEM_LOCALPART.match(localpart):
        return (True, f"system/bounce sender ({sender or 'unknown'})")

    from front_client import FrontClient
    body = " ".join(FrontClient.extract_plain_text_body(m) for m in inbound)
    if _BULK_BODY.search(body):
        return (True, "bulk/marketing footer (unsubscribe/preferences)")

    return (False, None)
