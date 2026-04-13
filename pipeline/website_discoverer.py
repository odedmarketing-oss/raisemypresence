"""
website_discoverer.py
Raise My Presence — Website Discovery Cascade

4-step cascade to find a business website when not in scan JSON:
  1. Places API `website` field (already in scan data)
  2. Scrape Google Maps listing page for website link
  3. Scrape directory sites (Yellow Pages AU/US, True Local AU, Yell UK)
  4. Google Search scrape by business name + city

Returns the first URL found, or None if all steps fail.
Architecture decision: drop the business from outreach if no website found
(email extraction requires a website to scrape).
"""

import re
import time
import logging
from urllib.parse import quote_plus, urlparse, urljoin

import requests
from bs4 import BeautifulSoup

from config import REQUEST_TIMEOUT, DISCOVERY_RATE_LIMIT

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Directory site configs by country
_DIRECTORIES = {
    "AU": [
        {
            "name": "Yellow Pages AU",
            "url_template": "https://www.yellowpages.com.au/find/{query}/{location}",
            "website_selector": "a.listing-content-link",
            "fallback_selector": 'a[href*="http"]',
        },
        {
            "name": "True Local",
            "url_template": "https://www.truelocal.com.au/find/{query}/{location}",
            "website_selector": "a.website-button",
            "fallback_selector": 'a[data-event="website"]',
        },
    ],
    "NZ": [
        {
            "name": "Yellow Pages NZ",
            "url_template": "https://www.yellow.co.nz/search/{query}/{location}",
            "website_selector": "a.listing-website",
            "fallback_selector": 'a[href*="http"]',
        },
    ],
    "GB": [
        {
            "name": "Yell",
            "url_template": "https://www.yell.com/ucs/UcsSearchAction.do?keywords={query}&location={location}",
            "website_selector": "a.btn--website",
            "fallback_selector": 'a[data-tracking="website"]',
        },
    ],
    "US": [
        {
            "name": "Yellow Pages US",
            "url_template": "https://www.yellowpages.com/search?search_terms={query}&geo_location_terms={location}",
            "website_selector": "a.track-visit-website",
            "fallback_selector": 'a.business-name',
        },
    ],
}


def _fetch(url: str, timeout: int = None) -> str | None:
    """Fetch a page. Returns HTML string or None."""
    try:
        resp = requests.get(
            url,
            headers=_HEADERS,
            timeout=timeout or REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None
        return resp.text[:2_000_000]
    except requests.RequestException as e:
        logger.debug(f"Fetch failed: {url} — {e}")
        return None


def _is_valid_website(url: str) -> bool:
    """Check if a URL looks like a real business website (not a directory/social page)."""
    if not url:
        return False
    skip_domains = (
        "google.com", "maps.google", "facebook.com", "instagram.com",
        "twitter.com", "x.com", "linkedin.com", "youtube.com",
        "yelp.com", "yellowpages.com", "yellowpages.com.au",
        "truelocal.com.au", "yell.com", "yellow.co.nz",
        "tripadvisor.com", "booking.com",
    )
    parsed = urlparse(url)
    domain = parsed.netloc.lower().lstrip("www.")
    for skip in skip_domains:
        if domain == skip or domain.endswith("." + skip):
            return False
    return True


def _rate_limit():
    """Sleep to respect rate limiting between external requests."""
    time.sleep(DISCOVERY_RATE_LIMIT)


# ---------------------------------------------------------------------------
# Step 1: Check scan JSON
# ---------------------------------------------------------------------------

def _step1_scan_json(business: dict) -> str | None:
    """Check if the scanner already found a website via Places API."""
    website = business.get("website", "")
    if website and _is_valid_website(website):
        logger.debug(f"Step 1 hit: website from scan JSON — {website}")
        return website
    return None


# ---------------------------------------------------------------------------
# Step 2: Scrape Google Maps listing page
# ---------------------------------------------------------------------------

def _step2_google_maps(business: dict) -> str | None:
    """
    Try to scrape the Google Maps listing page for a website link.
    Note: Maps pages are JS-rendered so this has a low success rate.
    Kept as a quick check before falling back to directories.
    """
    maps_url = business.get("google_maps_url", "")
    if not maps_url:
        return None

    _rate_limit()
    html = _fetch(maps_url)
    if not html:
        return None

    # Look for website URLs in the raw HTML (sometimes present in SSR data)
    # Google Maps embeds some structured data even before JS renders
    soup = BeautifulSoup(html, "html.parser")

    # Check for website links in button-like elements
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "url?q=" in href:
            # Google redirect URL: /url?q=https://actual-website.com&...
            match = re.search(r"url\?q=(https?://[^&]+)", href)
            if match:
                candidate = match.group(1)
                if _is_valid_website(candidate):
                    logger.debug(f"Step 2 hit: Google Maps page — {candidate}")
                    return candidate

    return None


# ---------------------------------------------------------------------------
# Step 3: Directory sites (Yellow Pages, True Local, Yell)
# ---------------------------------------------------------------------------

def _detect_country(business: dict) -> str:
    """
    Detect country from scan data.
    Checks address for known country patterns, falls back to 'US'.
    """
    address = business.get("address", "").lower()
    city = business.get("city", "").lower()

    # AU detection
    au_states = ["qld", "nsw", "vic", "sa", "wa", "tas", "act", "nt",
                 "queensland", "new south wales", "victoria", "south australia",
                 "western australia", "tasmania", "australia"]
    if any(s in address for s in au_states):
        return "AU"

    # NZ detection
    nz_markers = ["new zealand", "auckland", "wellington", "christchurch",
                  "nz ", " nz"]
    if any(s in address for s in nz_markers):
        return "NZ"

    # UK detection
    uk_markers = ["united kingdom", "england", "scotland", "wales",
                  " uk", ",uk"]
    # UK postcodes: letter(s) + digit(s) + space + digit + letters
    uk_postcode = re.search(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", address, re.IGNORECASE)
    if any(s in address for s in uk_markers) or uk_postcode:
        return "GB"

    # Default US
    return "US"


def _step3_directories(business: dict) -> str | None:
    """Search directory sites for the business website."""
    name = business.get("name", "")
    city = business.get("city", "")
    if not name or not city:
        return None

    country = _detect_country(business)
    directories = _DIRECTORIES.get(country, _DIRECTORIES["US"])

    query = quote_plus(name)
    location = quote_plus(city)

    for directory in directories:
        url = directory["url_template"].format(query=query, location=location)
        _rate_limit()
        html = _fetch(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")

        # Try primary selector
        link = soup.select_one(directory["website_selector"])
        if link and link.get("href") and _is_valid_website(link["href"]):
            logger.debug(f"Step 3 hit: {directory['name']} — {link['href']}")
            return link["href"]

        # Try fallback selector
        link = soup.select_one(directory["fallback_selector"])
        if link and link.get("href") and _is_valid_website(link["href"]):
            logger.debug(f"Step 3 hit (fallback): {directory['name']} — {link['href']}")
            return link["href"]

    return None


# ---------------------------------------------------------------------------
# Step 4: Google Search scrape
# ---------------------------------------------------------------------------

def _step4_google_search(business: dict) -> str | None:
    """
    Last resort: Google Search for 'business name city website'.
    Fragile — Google blocks scrapers aggressively.
    """
    name = business.get("name", "")
    city = business.get("city", "")
    if not name:
        return None

    query = quote_plus(f"{name} {city} website")
    url = f"https://www.google.com/search?q={query}&num=5"

    _rate_limit()
    html = _fetch(url, timeout=8)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Google wraps results in <a> tags — look for non-Google URLs
    for a in soup.find_all("a", href=True):
        href = a["href"]

        # Direct links
        if href.startswith("http") and _is_valid_website(href):
            return href

        # Google redirect format: /url?q=https://...&sa=...
        if "/url?q=" in href:
            match = re.search(r"/url\?q=(https?://[^&]+)", href)
            if match:
                candidate = match.group(1)
                if _is_valid_website(candidate):
                    logger.debug(f"Step 4 hit: Google Search — {candidate}")
                    return candidate

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def discover_website(business: dict) -> str | None:
    """
    4-step cascade to find a business website.

    Args:
        business: dict from scanner JSON.

    Returns:
        Website URL string, or None if not found (business should be dropped).
    """
    name = business.get("name", "unknown")

    # Step 1: Scan JSON (Places API data — no network call)
    url = _step1_scan_json(business)
    if url:
        return url

    logger.info(f"No website in scan data for '{name}' — starting discovery cascade")

    # Step 2: Google Maps listing page
    url = _step2_google_maps(business)
    if url:
        return url

    # Step 3: Directory sites
    url = _step3_directories(business)
    if url:
        return url

    # Step 4: Google Search (last resort)
    url = _step4_google_search(business)
    if url:
        return url

    logger.info(f"No website found for '{name}' after full cascade — dropping")
    return None
