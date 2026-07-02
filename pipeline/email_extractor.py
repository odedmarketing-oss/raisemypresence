"""
email_extractor.py
Raise My Presence — Email Extraction from Websites

Fetches a website and extracts real email addresses found on the page.
Two extraction methods:
  1. mailto: links in href attributes
  2. Email regex pattern matching in visible text and meta tags

Rules:
  - No pattern guessing (no info@, contact@, hello@ fabrication)
  - Only returns emails actually present on the page
  - Filters out common noreply/automated addresses
  - Deduplicates and lowercases all results

Telemetry mode (RMP #32):
  - extract_emails(url, return_telemetry=True) returns (emails, diagnostics)
  - Production path (return_telemetry=False, default) is unchanged
"""

import re
import logging
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup

from config import REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# Email regex — same RFC 5322 simplified as validator
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+",
    re.IGNORECASE,
)

# Obfuscation telemetry (RMP #32) — counts " at " / "[at]" / "(at)" within
# ~50 chars of " dot " / "[dot]" / "(dot)". Signal only, not used for
# extraction. False positives acceptable; goal is bucket detection.
_OBFUSCATION_RE = re.compile(
    r"[\[\(\s]at[\]\)\s].{1,50}[\[\(\s]dot[\]\)\s]",
    re.IGNORECASE,
)

# Addresses to always skip — automated/noreply/example
_SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "abuse", "webmaster",
    "root@", "admin@localhost",
    # RMP #33 Phase 2.1: placeholder local-parts
    "example", "your-business",
)

_SKIP_DOMAINS = (
    "example.com", "example.org", "example.net",
    "sentry.io", "wixpress.com", "wordpress.com",
    "squarespace.com", "googleapis.com", "google.com",
    "facebook.com", "twitter.com", "instagram.com",
    # RMP #33 Phase 2.1: placeholder domains
    "yourdomain.com", "youremail.com", "your-business.com",
    # RMP #82 B1: booking/scheduling platform domains — these are platform
    # inboxes (e.g. safeguarding@vagaro.com), not the business owner.
    "vagaro.com", "booksy.com", "fresha.com", "genbook.com",
    "schedulicity.com", "styleseat.com", "boulevard.io",
    "mindbodyonline.com", "acuityscheduling.com", "setmore.com",
    "glossgenius.com", "mangomint.com", "zenoti.com",
    "phorest.com", "salonrunner.com", "rosy.com",
    "salonbiz.com", "saloninteractive.com", "shortcuts.com.au",
    "squareup.com", "square.site",
)

# Common image/asset extensions to ignore in email-like strings
_ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RMPBot/1.0; +https://raisemypresence.com)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def _should_skip(email: str) -> bool:
    """Filter out noreply, automated, and non-business addresses."""
    email = email.lower()
    local = email.split("@")[0]
    domain = email.split("@")[1] if "@" in email else ""

    for prefix in _SKIP_PREFIXES:
        if local.startswith(prefix):
            return True

    for skip_domain in _SKIP_DOMAINS:
        if domain == skip_domain or domain.endswith("." + skip_domain):
            return True

    # Skip if it looks like a file path accidentally matched
    for ext in _ASSET_EXTENSIONS:
        if ext in email:
            return True

    return False


def _extract_mailto(soup: BeautifulSoup) -> set[str]:
    """Extract emails from mailto: href attributes."""
    emails = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.lower().startswith("mailto:"):
            # Strip mailto: prefix and any query params (?subject=...)
            raw = unquote(href[7:]).split("?")[0].strip()
            if raw and "@" in raw:
                emails.add(raw.lower())
    return emails


def _extract_regex(text: str) -> set[str]:
    """Extract emails from raw text via regex."""
    return {m.lower() for m in _EMAIL_RE.findall(text)}


def _count_obfuscation(text: str) -> int:
    """Count ' at ... dot ' obfuscation patterns within ~50 chars.
    Telemetry signal (RMP #32); not used for extraction."""
    return len(_OBFUSCATION_RE.findall(text))


def _fetch_page(url: str, telemetry_record: dict | None = None) -> str | None:
    """
    Fetch a URL and return HTML text, or None on failure.
    Follows redirects, respects timeout, caps response size at 2MB.

    If telemetry_record (dict) is provided, populates in-place with:
      - http_status: int | None
      - page_size: int (bytes read before cap)
      - exception: str | None (exception type name on failure)
    """
    try:
        resp = requests.get(
            url,
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        if telemetry_record is not None:
            telemetry_record["http_status"] = resp.status_code
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            logger.debug(f"Skipping non-HTML content-type: {content_type}")
            return None

        # Read up to 2MB
        chunks = []
        size = 0
        for chunk in resp.iter_content(chunk_size=8192, decode_unicode=True):
            chunks.append(chunk)
            size += len(chunk)
            if size > 2_000_000:
                break

        if telemetry_record is not None:
            telemetry_record["page_size"] = size

        return "".join(chunks)

    except requests.RequestException as e:
        if telemetry_record is not None:
            # RMP #34 Phase 2.2: preserve pre-raise http_status (line ~142
            # captures status for any response returned, incl. 4xx/5xx before
            # raise_for_status()). Fall back to e.response only if pre-raise
            # didn't run. http_status=None now means true pre-connect failure
            # (DNS/SSL/timeout/conn refused) — clean dead-site signal.
            if telemetry_record.get("http_status") is None:
                resp_obj = getattr(e, "response", None)
                if resp_obj is not None:
                    telemetry_record["http_status"] = resp_obj.status_code
            telemetry_record["exception"] = type(e).__name__
        logger.debug(f"Failed to fetch {url}: {e}")
        return None


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_emails(
    website_url: str,
    return_telemetry: bool = False,
) -> list[str] | tuple[list[str], dict]:
    """
    Extract email addresses from a website.

    Checks homepage first. If no emails found, also checks /contact
    and /contact-us, /about, /about-us pages.

    Args:
        website_url: The business website URL.
        return_telemetry: If True, also return a diagnostics dict with
            per-page signals (http_status, page_size, text_length,
            mailto_count, has_form, obfuscation_hits) and rollups
            (total_mailto_count, total_obfuscation_hits, early_break,
            found_count). Default False preserves the original list[str]
            return for all production callers.

    Returns:
        return_telemetry=False (default): list[str] — validated, deduplicated
            email addresses (unchanged production behavior).
        return_telemetry=True: tuple[list[str], dict].
    """
    url = _normalize_url(website_url)
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    all_emails: set[str] = set()
    pages_telemetry: list[dict] = []
    total_mailto = 0
    total_obfuscation = 0
    early_break = False

    # Pages to check — homepage first, then common contact pages.
    # RMP #33 Phase 2.1: extended subpath list targets Bucket B
    # (contact-form-only sites) — staff/team directories often expose
    # individual emails when /contact is form-only.
    pages = [url]
    for path in [
        "/contact", "/contact-us",
        "/team", "/staff", "/people", "/our-doctors", "/our-team",
        "/about", "/about-us",
    ]:
        pages.append(base + path)

    for page_url in pages:
        page_record: dict = {
            "url": page_url,
            "http_status": None,
            "page_size": 0,
            "text_length": 0,
            "mailto_count": 0,
            "has_form": False,
            "obfuscation_hits": 0,
            "exception": None,
        }

        html = _fetch_page(
            page_url,
            telemetry_record=page_record if return_telemetry else None,
        )

        if not html:
            if return_telemetry:
                pages_telemetry.append(page_record)
            continue

        soup = BeautifulSoup(html, "html.parser")

        # Method 1: mailto links
        page_mailto = _extract_mailto(soup)
        all_emails.update(page_mailto)

        # Method 2: regex on visible text
        text = soup.get_text(separator=" ", strip=True)
        all_emails.update(_extract_regex(text))

        # Method 3: regex on meta tags (some sites put email in meta)
        for meta in soup.find_all("meta", attrs={"content": True}):
            all_emails.update(_extract_regex(meta["content"]))

        if return_telemetry:
            page_record["text_length"] = len(text)
            page_record["mailto_count"] = len(page_mailto)
            page_record["has_form"] = soup.find("form") is not None
            page_record["obfuscation_hits"] = _count_obfuscation(text)
            total_mailto += page_record["mailto_count"]
            total_obfuscation += page_record["obfuscation_hits"]
            pages_telemetry.append(page_record)

        # If we found emails on this page, no need to check deeper pages
        filtered = {e for e in all_emails if not _should_skip(e)}
        if filtered:
            early_break = (page_url != pages[-1])
            break

    # Final filter and sort
    result = sorted(e for e in all_emails if not _should_skip(e))

    if result:
        logger.info(f"Found {len(result)} email(s) on {url}: {result}")
    else:
        logger.debug(f"No emails found on {url}")

    if return_telemetry:
        telemetry = {
            "pages": pages_telemetry,
            "total_mailto_count": total_mailto,
            "total_obfuscation_hits": total_obfuscation,
            "early_break": early_break,
            "found_count": len(result),
        }
        return result, telemetry

    return result
