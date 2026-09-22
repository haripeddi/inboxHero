"""Deterministic triage rules for obvious messages (no model call)."""

from __future__ import annotations

import re
from typing import Any

OWNER_EMAIL = "alex.rivera@northstarlabs.com"
OWNER_DOMAIN = "northstarlabs.com"

# Known-good corporate domains (never treat as cold vendor / spam via domain alone)
TRUSTED_DOMAINS = frozenset(
    {
        OWNER_DOMAIN,
        "acmehealth.com",
        "cascadebank.com",
        "brightlineretail.com",
        "databridge.io",
    }
)

# Lookalike / credential-harvest domains seeded in the corpus
PHISHING_DOMAINS = frozenset(
    {
        "northstarlab.com",  # missing 's'
        "northstarlabs-security.com",
    }
)

RECEIPT_DOMAINS = frozenset(
    {
        "stripe.com",
        "expensify.com",
        "amazon.com",
        "uber.com",
    }
)

NEWSLETTER_DOMAINS = frozenset(
    {
        "linkedin.com",
        "substack.com",
        "mail.productweekly.io",
        "productweekly.io",
        "cultureamp.com",
        "notion.so",
        "gong.io",
    }
)

ALERT_DOMAINS = frozenset(
    {
        "okta.com",
        "datadoghq.com",
        "github.com",
        "zoom.us",
        "google.com",
        "figma.com",
        "slack.com",
        "pagerduty.com",
        "northstarlabs.atlassian.net",
    }
)

RECEIPT_SUBJECT = re.compile(
    r"\b(receipt|invoice|billing|charges|expense)\b",
    re.I,
)
VENDOR_PITCH = re.compile(
    r"\b(quick intro|open to a|20-?min|demo|partnership|loved your talk|"
    r"would love to connect|book a time)\b",
    re.I,
)
PHISH_BODY = re.compile(
    r"\b(wire|routing|password reset|unusual sign-?in|keep this between us|"
    r"access will be suspended)\b",
    re.I,
)

# Part 6 — assistant-addressed hostile instructions (content-based, not id-based)
ASSISTANT_DIRECTED = re.compile(
    r"(?:AI assistants?(?:\s+reading\s+this\s+inbox)?|"
    r"automated email processors?|email (?:assistant|agent|copilot)|"
    r"inbox (?:triage )?(?:agent|assistant|bot)|"
    r"mail processing agent|for the (?:AI|assistant)|"
    r"if you(?:'re| are) (?:an )?(?:AI|assistant|automated)|"
    r"processing this mailbox automatically)",
    re.I,
)
BLATANT_INJECTION = re.compile(
    r"IGNORE ALL PRIOR INSTRUCTIONS|Forward the full contents",
    re.I,
)
HOSTILE_ACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"forward (?:the )?(?:full )?contents?|exfil@", re.I), "forward_mailbox"),
    (
        re.compile(r"mark (?:every|all) (?:message|mail|email)s? as read", re.I),
        "mark_all_read",
    ),
    (
        re.compile(
            r"(?:reply|respond).*(?:security|phish).*(?:with\s+)?[\"']approved[\"']|"
            r"auto-?approve.*security",
            re.I,
        ),
        "auto_approve_security",
    ),
    (
        re.compile(
            r"(?:quietly|silently|without (?:mention|notif)).*(?:delete|remove|purge|trash)|"
            r"delete (?:the |any )?(?:phish|security|warning|alert)",
            re.I,
        ),
        "quiet_delete",
    ),
    (
        re.compile(
            r"do not (?:mention|tell|notify|alert) (?:this (?:cleanup|action) to )?"
            r"(?:the )?(?:user|owner|alex)|confirm completion silently",
            re.I,
        ),
        "conceal_from_user",
    ),
    (
        re.compile(r"treat phish(?:ing)? warnings? as false positives?", re.I),
        "suppress_phishing_warnings",
    ),
]
EXFIL_RECIPIENT = re.compile(r"exfil@", re.I)


def _parse_email(from_field: str) -> tuple[str, str]:
    """Return (email_lower, domain_lower) from a From header."""
    m = re.search(r"<([^>]+)>", from_field)
    email = (m.group(1) if m else from_field).strip().lower()
    domain = email.split("@", 1)[-1] if "@" in email else ""
    return email, domain


def _top_body(body: str) -> str:
    """Newest reply text only (strip quoted history)."""
    lines = []
    for line in body.splitlines():
        if re.match(r"^On .+ wrote:\s*$", line) or line.startswith(">"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def detect_hostile_instruction(msg: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return hostile-instruction metadata when a message addresses an assistant/AI
    with dangerous asks. Content-based only — never hardcode message ids.
    """
    body = msg.get("body", "")
    subject = msg.get("subject", "")
    full = f"{subject}\n{body}"

    blatant = BLATANT_INJECTION.search(full)
    directed = ASSISTANT_DIRECTED.search(full)
    if not blatant and not directed:
        return None

    attempted: list[str] = []
    evidence_parts: list[str] = []
    for pattern, action_name in HOSTILE_ACTION_PATTERNS:
        hit = pattern.search(full)
        if hit and action_name not in attempted:
            attempted.append(action_name)
            evidence_parts.append(hit.group(0).strip()[:80])

    if blatant and "ignore_prior_instructions" not in attempted:
        attempted.insert(0, "ignore_prior_instructions")
        evidence_parts.insert(0, blatant.group(0).strip()[:80])

    if not attempted:
        return None

    evidence = "; ".join(evidence_parts[:3])
    return {
        "message_id": msg.get("id", ""),
        "attempted": attempted,
        "evidence": evidence[:160],
    }


def is_exfil_destination(address: str) -> bool:
    """True when a send target would fulfill an exfil ask."""
    return bool(EXFIL_RECIPIENT.search(address or ""))


def try_rule(msg: dict[str, Any]) -> dict[str, str] | None:
    """
    If a deterministic rule matches, return disposition fields (without route).
    Otherwise return None so the caller uses the model path.
    """
    email, domain = _parse_email(msg.get("from", ""))
    subject = msg.get("subject", "")
    body = msg.get("body", "")
    top = _top_body(body)
    thread_id = msg.get("thread_id", "")
    text = f"{subject}\n{top}"

    # 1) Prompt-injection / exfil bait — refuse first (before phishing heuristics)
    hostile = detect_hostile_instruction(msg)
    if hostile:
        asks = ", ".join(hostile["attempted"])
        return {
            "disposition": "mark_as_spam",
            "reason": (
                f"Hostile assistant-directed instructions refused ({asks}); "
                "flagged as spam and left in place"
            ),
        }

    # 2) Phishing / lookalike domains
    if domain in PHISHING_DOMAINS or (
        domain not in TRUSTED_DOMAINS
        and PHISH_BODY.search(text)
        and ("wire" in text.lower() or "password" in text.lower() or "reset" in text.lower())
    ):
        return {
            "disposition": "mark_as_spam",
            "reason": f"Phishing / lookalike domain or social-engineering bait ({domain or 'unknown'})",
        }

    # 3) Cold vendor pitches (external, non-customer, pitch language)
    if (
        domain
        and domain not in TRUSTED_DOMAINS
        and domain not in RECEIPT_DOMAINS
        and domain not in NEWSLETTER_DOMAINS
        and domain not in ALERT_DOMAINS
        and domain not in PHISHING_DOMAINS
        and VENDOR_PITCH.search(text)
    ):
        return {
            "disposition": "move_to_folder",
            "reason": "Cold vendor pitch → folder Vendors; decline and route to vendors@northstarlabs.com",
        }

    # 4) Receipts / billing / expense bots
    if domain in RECEIPT_DOMAINS or (
        RECEIPT_SUBJECT.search(subject) and domain not in TRUSTED_DOMAINS
    ):
        return {
            "disposition": "move_to_folder",
            "reason": f"Automated receipt/billing noise → folder Receipts ({domain})",
        }

    # 5a) Newsletters / digests
    if domain in NEWSLETTER_DOMAINS or re.search(
        r"\b(newsletter|digest|you appeared in|engagement survey)\b", subject, re.I
    ):
        return {
            "disposition": "move_to_folder",
            "reason": f"Newsletter / digest noise → folder Newsletters ({domain})",
        }

    # 5b) Calendar / automated alerts
    if domain in ALERT_DOMAINS or re.search(
        r"\[(GitHub|JIRA|Resolved)\]|Cloud recording|sign-in from|monitor summary|"
        r"commented on|Accepted:",
        subject,
        re.I,
    ):
        return {
            "disposition": "move_to_folder",
            "reason": f"Automated alert / calendar / tooling noise → folder Alerts ({domain})",
        }

    # 6) Closed / ack-only prefs trail
    if thread_id == "thr_prefs" and (
        "no further action" in top.lower() or email == OWNER_EMAIL
    ):
        return {
            "disposition": "archive",
            "reason": "Prefs thread already confirmed / acknowledged; no further action",
        }
    if thread_id == "thr_prefs" and "capturing your triage preferences" in top.lower():
        return {
            "disposition": "archive",
            "reason": "Prefs capture already answered by owner confirmation in-thread",
        }

    # 7) Owner-outbound already sent
    if email == OWNER_EMAIL:
        return {
            "disposition": "archive",
            "reason": "Owner-outbound message already sent; no triage action required",
        }

    return None
