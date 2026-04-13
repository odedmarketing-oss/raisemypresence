"""
email_validator.py
Raise My Presence — Email Validation

Two-stage validation before any email is sent:
  1. Syntax check (RFC 5322 simplified regex)
  2. MX record DNS lookup — confirms the domain has a mail server

No SMTP verification (too slow, often blocked, privacy concerns).
No pattern guessing — this module only validates, never generates.
"""

import re
import dns.resolver

# RFC 5322 simplified — covers 99.9% of real-world addresses
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)

# Cache MX results per domain within a single pipeline run
_mx_cache: dict[str, bool] = {}


def is_valid_syntax(email: str) -> bool:
    """Check email against RFC 5322 simplified pattern."""
    if not email or len(email) > 254:
        return False
    return _EMAIL_RE.match(email.strip().lower()) is not None


def has_mx_record(domain: str, timeout: float = 5.0) -> bool:
    """
    Check if domain has MX records via DNS lookup.
    Falls back to A record check (some domains serve mail without MX).
    Results cached per domain for the process lifetime.
    """
    domain = domain.lower().strip()

    if domain in _mx_cache:
        return _mx_cache[domain]

    result = False
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        result = len(answers) > 0
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        # No MX — try A record fallback
        try:
            answers = dns.resolver.resolve(domain, "A", lifetime=timeout)
            result = len(answers) > 0
        except Exception:
            result = False
    except Exception:
        result = False

    _mx_cache[domain] = result
    return result


def validate_email(email: str) -> tuple[bool, str]:
    """
    Full validation: syntax + MX.
    Returns (is_valid, reason).
    """
    email = email.strip().lower()

    if not is_valid_syntax(email):
        return False, "invalid_syntax"

    domain = email.split("@", 1)[1]

    if not has_mx_record(domain):
        return False, "no_mx_record"

    return True, "valid"


def clear_mx_cache():
    """Reset MX cache (useful between pipeline runs in testing)."""
    _mx_cache.clear()
