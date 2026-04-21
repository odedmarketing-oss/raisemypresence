"""
report_generator.py
Raise My Presence — Audit Report Generator v3

Generates a branded HTML audit report for a single business record.
Designed to be imported by the outreach pipeline — returns HTML as a string,
no file I/O, no external dependencies beyond Python stdlib.

v3 changes (Day 11):
  - Full table-based layout (email client compatible — Gmail, Outlook, Yahoo)
  - All styles inline (no dependency on <style> block)
  - Score breakdown values clamped to max (fixes overflow from old scanner data)
  - Hidden preheader text for inbox preview
  - Score circle rendered via table cell (not flexbox)
  - Recommendation badges rendered via table cells (not flexbox)
  - Progress bars thickened to 10px for reliable rendering
  - CTA copy reworked to match action (mailto)
  - Typography: 16px min body, proper hierarchy spacing
  - Whitespace: 24px+ around CTA, 40px+ section spacing
  - Subject line generation function
  - 4-locale system (US/UK/AU/NZ): spelling, compliance, credibility signals
  - Locale auto-detection from business address
  - Empty google_maps_url gracefully handled

Scoring weights sourced from:
  - Whitespark 2026 Local Search Ranking Factors (47 experts, 187 factors)
  - Google Business Profile official documentation
  - Localo 2M profile study

Usage:
    from report_generator import generate_report, generate_subject, detect_locale
    locale = detect_locale(business["address"])
    html = generate_report(business, recipient_email="owner@biz.com", locale=locale)
    subject = generate_subject(score, locale=locale)
"""

from datetime import date
from urllib.parse import quote
import re


# ---------------------------------------------------------------------------
# Scoring system — Whitespark 2026 sourced weights
# ---------------------------------------------------------------------------
#
# Factor weights reflect their known contribution to Local Pack ranking:
#   Categories  20 — #1 ranking factor per Whitespark, 6 years running
#   Reviews     18 — Part of ~20% review signal weight (Whitespark 2026)
#   Rating      12 — Top conversion factor (Whitespark)
#   Hours       15 — More influential than additional categories (Whitespark)
#   Description 12 — Google docs + Localo 2M profile study
#   Photos      12 — Engagement signal
#   Website      8 — NAP/relevance signal
#   Phone        3 — Basic NAP, near-universal, low differentiation
#   TOTAL      100

SCORE_FACTORS = [
    # (breakdown_key, display_name, max_points)
    ("categories",   "Business Categories",    20),
    ("reviews",      "Review Count",           18),
    ("rating",       "Star Rating",            12),
    ("hours",        "Business Hours",         15),
    ("description",  "Business Description",   12),
    ("photos",       "Photos",                 12),
    ("website",      "Website",                 8),
    ("phone",        "Phone Number",            3),
]

SCORE_FACTOR_MAP = {k: m for k, _, m in SCORE_FACTORS}


# ---------------------------------------------------------------------------
# Locale system — 4 markets (US, UK, AU, NZ)
# ---------------------------------------------------------------------------

LOCALES = {
    "US": {
        "optimised": "optimized",
        "optimise": "optimize",
        "prioritises": "prioritizes",
        "organised": "organized",
        "well_optimised": "well-optimized",
        "credibility": "BBB-aligned business practices",
        "compliance": "CAN-SPAM compliant",
        "compliance_law": "CAN-SPAM Act",
    },
    "UK": {
        "optimised": "optimised",
        "optimise": "optimise",
        "prioritises": "prioritises",
        "organised": "organised",
        "well_optimised": "well-optimised",
        "credibility": "Trading Standards compliant practices",
        "compliance": "GDPR &amp; PECR compliant",
        "compliance_law": "GDPR &amp; PECR",
    },
    "AU": {
        "optimised": "optimised",
        "optimise": "optimise",
        "prioritises": "prioritises",
        "organised": "organised",
        "well_optimised": "well-optimised",
        "credibility": "ACCC-aligned business practices",
        "compliance": "Spam Act 2003 compliant",
        "compliance_law": "Australian Spam Act 2003",
    },
    "NZ": {
        "optimised": "optimised",
        "optimise": "optimise",
        "prioritises": "prioritises",
        "organised": "organised",
        "well_optimised": "well-optimised",
        "credibility": "Commerce Commission compliant practices",
        "compliance": "Unsolicited Electronic Messages Act 2007 compliant",
        "compliance_law": "NZ Unsolicited Electronic Messages Act 2007",
    },
}


def detect_locale(address: str) -> str:
    """
    Detect locale from business address string.
    Scanner addresses end with country identifiers:
      - ", USA" -> US
      - ", UK" -> UK
      - ", Australia" -> AU
      - ", New Zealand" or ", NZ" -> NZ
    Fallback: US (largest volume market).
    """
    if not address:
        return "US"
    addr = address.strip().upper()
    if addr.endswith("USA") or addr.endswith("UNITED STATES"):
        return "US"
    if addr.endswith("UK") or addr.endswith("UNITED KINGDOM"):
        return "UK"
    if addr.endswith("NEW ZEALAND") or re.search(r",\s*NZ\s*$", addr):
        return "NZ"
    if addr.endswith("AUSTRALIA"):
        return "AU"
    return "US"


def recompute_score(breakdown: dict) -> int:
    """
    Always recompute the total score from breakdown values.
    Never trust the top-level completeness_score from the JSON —
    it may have been calculated with old weights.
    Clamps each factor to its maximum before summing.
    """
    total = 0
    for key, _, maximum in SCORE_FACTORS:
        earned = breakdown.get(key, 0)
        total += min(earned, maximum)
    return min(100, total)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _score_color(score: int) -> str:
    if score < 50:
        return "#DC2626"
    elif score < 70:
        return "#D97706"
    return "#16A34A"


def _score_label(score: int) -> str:
    if score < 50:
        return "Needs Significant Work"
    elif score < 70:
        return "Needs Improvement"
    return "Good"


def _factor_status(earned: int, maximum: int) -> tuple:
    """Returns (pill_text, pill_bg, pill_color)."""
    if earned == 0:
        return "Missing",    "#FEE2E2", "#DC2626"
    elif earned < maximum:
        return "Incomplete", "#FEF3C7", "#D97706"
    return "Complete",       "#DCFCE7", "#16A34A"


def _bar_color(earned: int, maximum: int) -> str:
    if earned == 0:
        return "#E5E7EB"
    pct = earned / maximum * 100 if maximum else 0
    if pct < 50:
        return "#DC2626"
    elif pct < 70:
        return "#D97706"
    return "#16A34A"


# ---------------------------------------------------------------------------
# Subject line generator
# ---------------------------------------------------------------------------

def generate_subject(score: int, locale: str = "US") -> str:
    """
    Generate a short, lowercase, internal-looking subject line.
    Based on cold-email skill: 2-4 words, no pitching, no punctuation tricks.
    Locale parameter reserved for future A/B testing per market.
    """
    if score < 30:
        return "your google profile"
    elif score < 50:
        return "google profile gaps"
    elif score < 70:
        return "profile quick wins"
    return "profile checkup"


# ---------------------------------------------------------------------------
# Recommendation engine — ordered by ranking impact (highest weight first)
# ---------------------------------------------------------------------------

def _build_recommendations(breakdown: dict, locale: str = "US") -> list:
    recs = []
    loc = LOCALES.get(locale, LOCALES["US"])

    cat = min(breakdown.get("categories", 0), SCORE_FACTOR_MAP["categories"])
    rev = min(breakdown.get("reviews", 0), SCORE_FACTOR_MAP["reviews"])
    rat = min(breakdown.get("rating", 0), SCORE_FACTOR_MAP["rating"])
    hrs = min(breakdown.get("hours", 0), SCORE_FACTOR_MAP["hours"])
    desc = min(breakdown.get("description", 0), SCORE_FACTOR_MAP["description"])
    pho = min(breakdown.get("photos", 0), SCORE_FACTOR_MAP["photos"])
    web = min(breakdown.get("website", 0), SCORE_FACTOR_MAP["website"])
    phn = min(breakdown.get("phone", 0), SCORE_FACTOR_MAP["phone"])

    if cat == 0:
        recs.append((
            "Set a Specific Business Category",
            "Your primary category is the single most important ranking factor on Google Maps "
            "(Whitespark 2026, confirmed #1 for six consecutive years). A missing or generic "
            "category means Google doesn&#8217;t know which searches to show you in. Set the most "
            "specific category that accurately describes your core service."
        ))
    elif cat < SCORE_FACTOR_MAP["categories"]:
        recs.append((
            "Refine Your Business Category",
            "Your category is set but not fully specific. Google ranks businesses that use "
            "precise categories &#8212; &#8216;Dental Clinic&#8217; outperforms &#8216;Health&#8217; every time. Review "
            "your primary and secondary categories against your actual services."
        ))

    if rev == 0:
        recs.append((
            "Get Your First Reviews",
            "Review signals account for approximately 20% of your Local Pack ranking weight "
            "(Whitespark 2026). Zero reviews signals an inactive business to Google. "
            "A direct personal ask from a recent customer has a 70%+ success rate."
        ))
    elif rev < SCORE_FACTOR_MAP["reviews"]:
        recs.append((
            "Build Review Velocity",
            "You have some reviews but not enough to compete. Businesses in top-3 positions "
            "average 200+ reviews with consistent monthly additions. Review recency now "
            "outweighs total count &#8212; a steady flow beats a one-time push."
        ))

    if rat == 0:
        recs.append((
            "Establish a Star Rating",
            "No rating means no trust signal. 68% of consumers only consider businesses rated "
            "4 stars or higher (BrightLocal 2026). Getting your first reviews establishes "
            "your baseline rating."
        ))
    elif rat < SCORE_FACTOR_MAP["rating"]:
        recs.append((
            "Improve Your Star Rating",
            "Your current rating is below the 4.0 threshold where most customers start "
            "considering a business. Responding to negative reviews and consistently requesting "
            "feedback from happy customers is the most reliable path to improvement."
        ))

    if hrs == 0:
        recs.append((
            "Add Your Business Hours",
            "Business hours are rated more influential than additional categories and review "
            f"count by local SEO experts (Whitespark 2026). Google {loc['prioritises']} businesses shown "
            "as open at the time of search. Missing hours makes you invisible for "
            "time-sensitive searches."
        ))

    if desc == 0:
        recs.append((
            "Write a Business Description",
            "Only 65% of businesses in positions 6-10 have completed descriptions &#8212; fewer than "
            "40% in positions 11-20 do (Localo, 2M profile study). A keyword-relevant "
            "description directly improves your relevance score with Google."
        ))

    if pho == 0:
        recs.append((
            "Upload Photos",
            "Profiles with photos receive significantly more clicks and direction requests. "
            "Google recommends 10+ photos covering your shopfront, interior, and work. "
            "Start with 3-5 high-quality images &#8212; any photos outperform none."
        ))
    elif pho < SCORE_FACTOR_MAP["photos"]:
        recs.append((
            "Add More Photos",
            "You have some photos but below the recommended threshold. Aim for 10+ covering "
            "different aspects of your business. Regular photo uploads also signal an active "
            "profile to Google."
        ))

    if web == 0:
        recs.append((
            "Link Your Website",
            "A website link reinforces your NAP consistency and gives Google a richer "
            "understanding of your business. It also drives direct traffic from customers "
            "who want to research before calling."
        ))

    if phn == 0:
        recs.append((
            "Add Your Phone Number",
            "A missing phone number prevents tap-to-call on mobile. 76-88% of local "
            "smartphone searches result in a store visit or call within a day "
            "(Google/Seoprofy). Make it easy for customers to reach you."
        ))

    return recs


# ---------------------------------------------------------------------------
# HTML builder — table-based, inline styles, email-client safe
# ---------------------------------------------------------------------------

def generate_report(business: dict, recipient_email: str = "", locale: str = "US") -> str:
    """
    Generate a branded HTML audit report for a single business.

    Args:
        business: dict matching the scanner JSON schema.
        recipient_email: If provided, unsubscribe link uses one-click
            webhook endpoint instead of mailto.
        locale: One of "US", "UK", "AU", "NZ". Controls spelling,
            compliance text, and credibility signals.

    Returns:
        str: Complete self-contained HTML document (table-based, inline styles).
    """
    loc             = LOCALES.get(locale, LOCALES["US"])
    name            = business.get("name", "Your Business")
    address         = business.get("address", "")
    google_maps_url = business.get("google_maps_url", "")
    maps_link_html = ""
    if google_maps_url and google_maps_url != "#":
        maps_link_html = f'<a href="{google_maps_url}" target="_blank" style="display:inline-block;margin-top:10px;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;font-size:13px;color:#16A34A;text-decoration:none;font-weight:500;">View on Google Maps &#8594;</a>'
    breakdown       = business.get("score_breakdown", {})
    report_date     = date.today().strftime("%B %d, %Y")

    score           = recompute_score(breakdown)
    color           = _score_color(score)
    label           = _score_label(score)
    recommendations = _build_recommendations(breakdown, locale=locale)

    # --- Preheader text (visible in inbox preview, hidden in email body) ---
    if score < 50:
        preheader = f"Your Google Business Profile scored {score}/100. Here are the highest-impact fixes."
    elif score < 70:
        preheader = f"Your Google Business Profile scored {score}/100. A few changes could improve your visibility."
    else:
        preheader = f"Your Google Business Profile scored {score}/100. Looking solid with room to grow."

    # --- Score breakdown rows ---
    rows_html = ""
    for key, display_name, maximum in SCORE_FACTORS:
        earned = min(breakdown.get(key, 0), maximum)  # CLAMP to max
        pill_text, pill_bg, pill_color = _factor_status(earned, maximum)
        fill_pct = int(earned / maximum * 100) if maximum else 0
        bar_col = _bar_color(earned, maximum)

        # Bar: outer track is always visible, inner fill proportional
        bar_fill_width = max(fill_pct, 0)

        rows_html += f"""
                        <tr>
                            <td style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#374151;padding:12px 0;width:38%;border-bottom:1px solid #F3F4F6;">{display_name}</td>
                            <td style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;color:#9CA3AF;width:12%;text-align:center;padding:12px 4px;white-space:nowrap;border-bottom:1px solid #F3F4F6;">{earned}/{maximum}</td>
                            <td style="width:30%;padding:12px 12px;border-bottom:1px solid #F3F4F6;">
                                <!--[if mso]>
                                <table cellpadding="0" cellspacing="0" width="100%"><tr><td style="background:#F3F4F6;height:10px;border-radius:5px;">
                                <table cellpadding="0" cellspacing="0" width="{bar_fill_width}%"><tr><td style="background:{bar_col};height:10px;border-radius:5px;">&nbsp;</td></tr></table>
                                </td></tr></table>
                                <![endif]-->
                                <!--[if !mso]><!-->
                                <div style="background:#F3F4F6;border-radius:5px;height:10px;overflow:hidden;">
                                    <div style="background:{bar_col};height:10px;border-radius:5px;width:{bar_fill_width}%;min-width:0;"></div>
                                </div>
                                <!--<![endif]-->
                            </td>
                            <td style="width:20%;text-align:right;padding:12px 0;border-bottom:1px solid #F3F4F6;">
                                <span style="display:inline-block;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:11px;font-weight:600;padding:3px 10px;border-radius:100px;white-space:nowrap;background:{pill_bg};color:{pill_color};">{pill_text}</span>
                            </td>
                        </tr>"""

    # --- Recommendation items ---
    recs_html = ""
    for i, (title, detail) in enumerate(recommendations, 1):
        recs_html += f"""
                        <tr>
                            <td style="padding:16px 0;border-bottom:1px solid #F3F4F6;vertical-align:top;width:44px;">
                                <table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
                                    <tr>
                                        <td width="32" height="32" align="center" valign="middle" style="width:32px;height:32px;border-radius:50%;background:#111827;color:#FFFFFF;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;font-weight:700;text-align:center;line-height:32px;">
                                            {i}
                                        </td>
                                    </tr>
                                </table>
                            </td>
                            <td style="padding:16px 0 16px 16px;border-bottom:1px solid #F3F4F6;vertical-align:top;">
                                <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:15px;font-weight:700;color:#111827;margin-bottom:6px;">{title}</div>
                                <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#6B7280;line-height:1.55;">{detail}</div>
                            </td>
                        </tr>"""

    if not recs_html:
        recs_html = f"""
                        <tr>
                            <td colspan="2" style="padding:16px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#6B7280;">
                                No critical issues found. Your profile is {loc['well_optimised']}.
                            </td>
                        </tr>"""

    urgency_line = (
        "There are critical gaps costing you visibility in local search."
        if score < 50 else
        "There are improvements available that could increase your local search visibility."
    )

    name_encoded = quote(name)

    # Build unsubscribe link — one-click webhook if recipient known, mailto fallback
    if recipient_email:
        unsub_url = f"https://webhooks.raisemypresence.com/webhook/unsubscribe?email={quote(recipient_email)}"
    else:
        unsub_url = f"mailto:hello@raisemypresence.com?subject=Unsubscribe%20-%20{name_encoded}"

    # CTA mailto
    cta_url = f"mailto:hello@raisemypresence.com?subject=Fix%20my%20Google%20profile%20-%20{name_encoded}"

    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <meta name="x-apple-disable-message-reformatting">
    <title>Google Profile Audit &#8212; {name}</title>
    <!--[if mso]>
    <noscript>
        <xml>
            <o:OfficeDocumentSettings>
                <o:PixelsPerInch>96</o:PixelsPerInch>
            </o:OfficeDocumentSettings>
        </xml>
    </noscript>
    <![endif]-->
    <style>
        /* Reset — clients that support <style> get cleaner rendering */
        body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
        table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
        img {{ -ms-interpolation-mode: bicubic; border: 0; height: auto; line-height: 100%; outline: none; text-decoration: none; }}
        body {{ margin: 0 !important; padding: 0 !important; width: 100% !important; }}
        /* Mobile adjustments */
        @media screen and (max-width: 480px) {{
            .email-container {{ width: 100% !important; }}
            .fluid {{ max-width: 100% !important; height: auto !important; }}
            .stack-column {{ display: block !important; width: 100% !important; }}
            .mobile-pad {{ padding-left: 24px !important; padding-right: 24px !important; }}
        }}
    </style>
</head>
<body style="margin:0;padding:0;background-color:#F3F4F6;width:100%;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">

    <!-- Preheader (inbox preview text, hidden in body) -->
    <div style="display:none;font-size:1px;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;mso-hide:all;">
        {preheader}
        &#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;
    </div>

    <!-- Outer wrapper table -->
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#F3F4F6;">
        <tr>
            <td align="center" style="padding:24px 16px;">

                <!-- Email container: 640px max -->
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" class="email-container" style="max-width:640px;width:100%;background-color:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5E7EB;">

                    <!-- ============ HEADER ============ -->
                    <tr>
                        <td style="background-color:#111827;padding:28px 40px;" class="mobile-pad">
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:18px;font-weight:700;color:#FFFFFF;letter-spacing:-0.02em;">
                                        Raise My <span style="color:#4ADE80;">Presence</span>
                                    </td>
                                    <td align="right" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:12px;color:#9CA3AF;line-height:1.6;">
                                        Google Profile Audit<br>{report_date}
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- ============ BUSINESS BLOCK ============ -->
                    <tr>
                        <td style="padding:32px 40px 24px;border-bottom:1px solid #F3F4F6;" class="mobile-pad">
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:#9CA3AF;margin-bottom:8px;">Audit prepared for</div>
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:24px;font-weight:700;color:#111827;margin-bottom:4px;line-height:1.25;">{name}</div>
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#6B7280;line-height:1.5;">{address}</div>
                            {maps_link_html}
                        </td>
                    </tr>

                    <!-- ============ SCORE CARD ============ -->
                    <tr>
                        <td style="padding:28px 40px 20px;" class="mobile-pad">
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border:2px solid {color};border-radius:12px;background-color:#FAFAFA;">
                                <tr>
                                    <td style="padding:24px 28px;">
                                        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                            <tr>
                                                <!-- Score circle -->
                                                <td width="96" valign="middle" style="width:96px;padding-right:24px;">
                                                    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
                                                        <tr>
                                                            <td width="88" height="88" align="center" valign="middle" style="width:88px;height:88px;border-radius:50%;border:5px solid {color};text-align:center;vertical-align:middle;">
                                                                <span style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:28px;font-weight:800;color:{color};line-height:1;">{score}</span>
                                                            </td>
                                                        </tr>
                                                    </table>
                                                </td>
                                                <!-- Score text -->
                                                <td valign="middle">
                                                    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:20px;font-weight:700;color:{color};margin-bottom:6px;">{label}</div>
                                                    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#6B7280;line-height:1.5;">
                                                        Your Google Business Profile scored <strong style="color:#111827;">{score}/100</strong>
                                                        on our completeness audit. {urgency_line}
                                                    </div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- ============ SCORING NOTE ============ -->
                    <tr>
                        <td style="padding:0 40px 28px;" class="mobile-pad">
                            <div style="padding:14px 18px;background-color:#F0FDF4;border-left:3px solid #16A34A;border-radius:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#4B5563;line-height:1.6;">
                                Score weights are based on the <span style="font-weight:700;color:#111827;">Whitespark 2026 Local Search Ranking Factors</span> report
                                (47 local SEO experts, 187 factors) and <span style="font-weight:700;color:#111827;">Google&#8217;s official GBP documentation</span>.
                                Factors are weighted by their known contribution to Local Pack ranking.
                            </div>
                        </td>
                    </tr>

                    <!-- ============ SCORE BREAKDOWN ============ -->
                    <tr>
                        <td style="padding:0 40px 36px;" class="mobile-pad">
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#374151;margin-bottom:16px;padding-bottom:10px;border-bottom:2px solid #F3F4F6;">
                                Score Breakdown
                            </div>
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
                                <tbody>{rows_html}
                                </tbody>
                            </table>
                        </td>
                    </tr>

                    <!-- ============ RECOMMENDATIONS ============ -->
                    <tr>
                        <td style="padding:0 40px 36px;" class="mobile-pad">
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#374151;margin-bottom:16px;padding-bottom:10px;border-bottom:2px solid #F3F4F6;">
                                Priority Fixes &#8212; Highest Impact First
                            </div>
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
                                <tbody>{recs_html}
                                </tbody>
                            </table>
                        </td>
                    </tr>

                    <!-- ============ CTA BLOCK ============ -->
                    <tr>
                        <td style="background-color:#F0FDF4;border-top:2px solid #DCFCE7;padding:36px 40px;text-align:center;" class="mobile-pad">
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:18px;font-weight:700;color:#111827;margin-bottom:8px;">
                                Want this fixed for you?
                            </div>
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#6B7280;margin-bottom:24px;max-width:440px;margin-left:auto;margin-right:auto;line-height:1.55;">
                                We handle everything &#8212; {loc['optimised']} description, Google Posts, photo guidelines,
                                and ongoing management. No calls, no meetings.
                            </div>
                            <!-- CTA Button — table-based for Outlook compatibility -->
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:0 auto;">
                                <tr>
                                    <td align="center" valign="middle" style="background-color:#16A34A;border-radius:8px;padding:14px 32px;">
                                        <a href="{cta_url}" target="_blank" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:15px;font-weight:600;color:#FFFFFF;text-decoration:none;display:inline-block;line-height:1;">
                                            Fix My Profile &#8594;
                                        </a>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- ============ FOOTER ============ -->
                    <tr>
                        <td style="padding:24px 40px;text-align:center;border-top:1px solid #F3F4F6;" class="mobile-pad">
                            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:12px;color:#9CA3AF;line-height:1.8;">
                                &#169; 2026 Raise My Presence &#183;
                                <a href="mailto:hello@raisemypresence.com" style="color:#9CA3AF;text-decoration:underline;">hello@raisemypresence.com</a><br>
                                You received this because your business was identified in a public Google Maps audit.
                                {loc['compliance']}.<br>
                                <a href="{unsub_url}" style="color:#9CA3AF;text-decoration:underline;">Unsubscribe</a>
                            </div>
                        </td>
                    </tr>

                </table>
                <!-- /Email container -->

            </td>
        </tr>
    </table>
    <!-- /Outer wrapper -->

</body>
</html>"""


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample = {
        "place_id": "ChIJtest123",
        "name": "Smith's Dental",
        "city": "Gold Coast",
        "vertical": "dentist",
        "address": "123 Main St, Gold Coast QLD 4217, Australia",
        "phone": "+61 7 1234 5678",
        "website": "https://smithsdental.com.au",
        "rating": 3.8,
        "review_count": 12,
        "photo_count": 3,
        "has_hours": True,
        "has_description": False,
        "has_website": True,
        "has_phone": True,
        "primary_type": "dentist",
        "google_maps_url": "https://maps.google.com/?cid=test",
        "completeness_score": 999,
        "score_breakdown": {
            "categories":   0,
            "reviews":      8,
            "rating":       7,
            "hours":       15,
            "description":  0,
            "photos":       6,
            "website":      8,
            "phone":        3,
        }
    }

    # --- Score tests ---
    html = generate_report(sample, locale="AU")
    expected_score = 0+8+7+15+0+6+8+3
    actual_score = recompute_score(sample["score_breakdown"])
    assert actual_score == expected_score
    html_no_ns = html.replace("w3.org/1999/xhtml", "")
    assert "999" not in html_no_ns
    assert str(actual_score) in html

    # --- Clamping tests ---
    overflow_sample = {
        "name": "Overflow Test",
        "address": "123 Test St, USA",
        "google_maps_url": "#",
        "score_breakdown": {
            "categories":  5,
            "reviews":     0,
            "rating":      0,
            "hours":       0,
            "description": 0,
            "photos":      0,
            "website":    15,
            "phone":      10,
        }
    }
    overflow_score = recompute_score(overflow_sample["score_breakdown"])
    assert overflow_score == 5 + 0 + 0 + 0 + 0 + 0 + 8 + 3
    overflow_html = generate_report(overflow_sample, locale="US")
    assert "15/8" not in overflow_html
    assert "10/3" not in overflow_html

    # --- Subject line tests ---
    assert generate_subject(17) == "your google profile"
    assert generate_subject(30) == "google profile gaps"
    assert generate_subject(55) == "profile quick wins"
    assert generate_subject(80) == "profile checkup"

    # --- Locale detection tests ---
    assert detect_locale("315 S Ohio Ave, Sedalia, MO 65301, USA") == "US"
    assert detect_locale("Shap Road Industrial Estate, Kendal LA9 6NZ, UK") == "UK"
    assert detect_locale("1,2/87 Willetts Rd, North Mackay QLD 4740, Australia") == "AU"
    assert detect_locale("123 Queen St, Hamilton 3204, New Zealand") == "NZ"
    assert detect_locale("") == "US"          # empty fallback
    assert detect_locale("Unknown Place") == "US"  # unrecognized fallback

    # --- Locale output tests ---
    us_html = generate_report(sample, locale="US")
    uk_html = generate_report(sample, locale="UK")
    au_html = generate_report(sample, locale="AU")
    nz_html = generate_report(sample, locale="NZ")

    # US uses American spelling
    assert "optimized description" in us_html
    assert "CAN-SPAM" in us_html

    # UK uses British spelling + GDPR
    assert "optimised description" in uk_html
    assert "GDPR" in uk_html

    # AU uses British spelling + Spam Act
    assert "optimised description" in au_html
    assert "Spam Act 2003" in au_html

    # NZ uses British spelling + NZ Act
    assert "optimised description" in nz_html
    assert "Unsolicited Electronic Messages Act 2007" in nz_html

    # --- Empty google_maps_url test ---
    no_maps_sample = {
        "name": "No Maps Business",
        "address": "123 Test St, UK",
        "google_maps_url": "",
        "score_breakdown": {
            "categories": 5, "reviews": 0, "rating": 0, "hours": 0,
            "description": 0, "photos": 0, "website": 0, "phone": 0,
        }
    }
    no_maps_html = generate_report(no_maps_sample, locale="UK")
    assert "View on Google Maps" not in no_maps_html

    # --- Generate all 4 locale previews ---
    for loc_code in ["US", "UK", "AU", "NZ"]:
        with open(f"/tmp/sample_report_v3_{loc_code}.html", "w") as f:
            f.write(generate_report(sample, recipient_email="test@example.com", locale=loc_code))

    print(f"Score recomputed correctly: {actual_score}/100")
    print(f"Overflow clamped correctly: {overflow_score}/100")
    print(f"Subject lines verified")
    print(f"Locale detection verified (US/UK/AU/NZ + fallbacks)")
    print(f"Locale outputs verified (spelling + compliance)")
    print(f"Empty google_maps_url verified")
    print(f"4 locale previews: /tmp/sample_report_v3_{{US,UK,AU,NZ}}.html")
