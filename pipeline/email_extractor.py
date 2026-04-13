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

# Addresses to always skip — automated/noreply/example
_SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "abuse", "webmaster",
    "root@", "admin@localhost",
)

_SKIP_DOMAINS = (
    "example.com", "example.org", "example.net",
    "sentry.io", "wixpress.com", "wordpress.com",
    "squarespace.com", "googleapis.com", "google.com",
    "facebook.com", "twitter.com", "instagram.com",
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


def _fetch_page(url: str) -> str | None:
    """
    Fetch a URL and return HTML text, or None on failure.
    Follows redirects, respects timeout, caps response size at 2MB.
    """
    try:
        resp = requests.get(
            url,
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
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

        return "".join(chunks)

    except requests.RequestException as e:
        logger.debug(f"Failed to fetch {url}: {e}")
        return None


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_emails(website_url: str) -> list[str]:
    """
    Extract email addresses from a website.

    Checks homepage first. If no emails found, also checks /contact
    and /contact-us, /about, /about-us pages.

    Args:
        website_url: The business website URL.

    Returns:
        List of validated, deduplicated email addresses found on the site.
        Empty list if none found or site unreachable.
    """
    url = _normalize_url(website_url)
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    all_emails: set[str] = set()

    # Pages to check — homepage first, then common contact pages
    pages = [url]
    for path in ["/contact", "/contact-us", "/about", "/about-us"]:
        pages.append(base + path)

    for page_url in pages:
        html = _fetch_page(page_url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")

        # Method 1: mailto links
        all_emails.update(_extract_mailto(soup))

        # Method 2: regex on visible text
        text = soup.get_text(separator=" ", strip=True)
        all_emails.update(_extract_regex(text))

        # Method 3: regex on meta tags (some sites put email in meta)
        for meta in soup.find_all("meta", attrs={"content": True}):
            all_emails.update(_extract_regex(meta["content"]))

        # If we found emails on this page, no need to check deeper pages
        filtered = {e for e in all_emails if not _should_skip(e)}
        if filtered:
            break

    # Final filter and sort
    result = sorted(e for e in all_emails if not _should_skip(e))

    if result:
        logger.info(f"Found {len(result)} email(s) on {url}: {result}")
    else:
        logger.debug(f"No emails found on {url}")

    return result
