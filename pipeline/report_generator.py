"""
report_generator.py
Raise My Presence — Audit Report Generator v2

Generates a branded HTML audit report for a single business record.
Designed to be imported by the outreach pipeline — returns HTML as a string,
no file I/O, no external dependencies beyond Python stdlib.

Scoring weights sourced from:
  - Whitespark 2026 Local Search Ranking Factors (47 experts, 187 factors)
  - Google Business Profile official documentation
  - Localo 2M profile study

Usage:
    from report_generator import generate_report
    html = generate_report(business, recipient_email="owner@biz.com")
"""

from datetime import date
from urllib.parse import quote


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


def recompute_score(breakdown: dict) -> int:
    """
    Always recompute the total score from breakdown values.
    Never trust the top-level completeness_score from the JSON —
    it may have been calculated with old weights.
    """
    return min(100, sum(breakdown.get(k, 0) for k, _, m in SCORE_FACTORS))


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _score_color(score: int) -> str:
    if score < 50:
        return "#EF4444"
    elif score < 70:
        return "#F59E0B"
    return "#16A34A"


def _score_label(score: int) -> str:
    if score < 50:
        return "Needs Significant Work"
    elif score < 70:
        return "Needs Improvement"
    return "Good"


def _factor_status(earned: int, maximum: int) -> tuple:
    if earned == 0:
        return "Missing",    "#FEE2E2", "#DC2626"
    elif earned < maximum:
        return "Incomplete", "#FEF3C7", "#D97706"
    return "Complete",       "#DCFCE7", "#16A34A"


# ---------------------------------------------------------------------------
# Recommendation engine — ordered by ranking impact (highest weight first)
# ---------------------------------------------------------------------------

def _build_recommendations(breakdown: dict) -> list:
    recs = []

    if breakdown.get("categories", 0) == 0:
        recs.append((
            "Set a Specific Business Category",
            "Your primary category is the single most important ranking factor on Google Maps "
            "(Whitespark 2026, confirmed #1 for six consecutive years). A missing or generic "
            "category means Google doesn't know which searches to show you in. Set the most "
            "specific category that accurately describes your core service."
        ))
    elif breakdown.get("categories", 0) < SCORE_FACTOR_MAP["categories"]:
        recs.append((
            "Refine Your Business Category",
            "Your category is set but not fully specific. Google ranks businesses that use "
            "precise categories — 'Dental Clinic' outperforms 'Health' every time. Review "
            "your primary and secondary categories against your actual services."
        ))

    if breakdown.get("reviews", 0) == 0:
        recs.append((
            "Get Your First Reviews",
            "Review signals account for approximately 20% of your Local Pack ranking weight "
            "(Whitespark 2026). Zero reviews signals an inactive business to Google. "
            "A direct personal ask from a recent customer has a 70%+ success rate."
        ))
    elif breakdown.get("reviews", 0) < SCORE_FACTOR_MAP["reviews"]:
        recs.append((
            "Build Review Velocity",
            "You have some reviews but not enough to compete. Businesses in top-3 positions "
            "average 200+ reviews with consistent monthly additions. Review recency now "
            "outweighs total count — a steady flow beats a one-time push."
        ))

    if breakdown.get("rating", 0) == 0:
        recs.append((
            "Establish a Star Rating",
            "No rating means no trust signal. 68% of consumers only consider businesses rated "
            "4 stars or higher (BrightLocal 2026). Getting your first reviews establishes "
            "your baseline rating."
        ))
    elif breakdown.get("rating", 0) < SCORE_FACTOR_MAP["rating"]:
        recs.append((
            "Improve Your Star Rating",
            "Your current rating is below the 4.0 threshold where most customers start "
            "considering a business. Responding to negative reviews and consistently requesting "
            "feedback from happy customers is the most reliable path to improvement."
        ))

    if breakdown.get("hours", 0) == 0:
        recs.append((
            "Add Your Business Hours",
            "Business hours are rated more influential than additional categories and review "
            "count by local SEO experts (Whitespark 2026). Google prioritises businesses shown "
            "as open at the time of search. Missing hours makes you invisible for "
            "time-sensitive searches."
        ))

    if breakdown.get("description", 0) == 0:
        recs.append((
            "Write a Business Description",
            "Only 65% of businesses in positions 6-10 have completed descriptions — fewer than "
            "40% in positions 11-20 do (Localo, 2M profile study). A keyword-relevant "
            "description directly improves your relevance score with Google."
        ))

    if breakdown.get("photos", 0) == 0:
        recs.append((
            "Upload Photos",
            "Profiles with photos receive significantly more clicks and direction requests. "
            "Google recommends 10+ photos covering your shopfront, interior, and work. "
            "Start with 3-5 high-quality images — any photos outperform none."
        ))
    elif breakdown.get("photos", 0) < SCORE_FACTOR_MAP["photos"]:
        recs.append((
            "Add More Photos",
            "You have some photos but below the recommended threshold. Aim for 10+ covering "
            "different aspects of your business. Regular photo uploads also signal an active "
            "profile to Google."
        ))

    if breakdown.get("website", 0) == 0:
        recs.append((
            "Link Your Website",
            "A website link reinforces your NAP consistency and gives Google a richer "
            "understanding of your business. It also drives direct traffic from customers "
            "who want to research before calling."
        ))

    if breakdown.get("phone", 0) == 0:
        recs.append((
            "Add Your Phone Number",
            "A missing phone number prevents tap-to-call on mobile. 76-88% of local "
            "smartphone searches result in a store visit or call within a day "
            "(Google/Seoprofy). Make it easy for customers to reach you."
        ))

    return recs


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def generate_report(business: dict, recipient_email: str = "") -> str:
    """
    Generate a branded HTML audit report for a single business.

    Args:
        business: dict matching the scanner JSON schema.
        recipient_email: If provided, unsubscribe link uses one-click
            webhook endpoint instead of mailto.

    Returns:
        str: Complete self-contained HTML document.
    """
    name            = business.get("name", "Your Business")
    address         = business.get("address", "")
    google_maps_url = business.get("google_maps_url", "#")
    breakdown       = business.get("score_breakdown", {})
    report_date     = date.today().strftime("%B %d, %Y")

    score           = recompute_score(breakdown)
    color           = _score_color(score)
    label           = _score_label(score)
    recommendations = _build_recommendations(breakdown)

    rows_html = ""
    for key, display_name, maximum in SCORE_FACTORS:
        earned = breakdown.get(key, 0)
        pill_text, pill_bg, pill_color = _factor_status(earned, maximum)
        fill_pct = int(earned / maximum * 100) if maximum else 0
        bar_color = _score_color(fill_pct)
        rows_html += f"""
                    <tr>
                        <td class="factor-name">{display_name}</td>
                        <td class="factor-score">{earned}&thinsp;/&thinsp;{maximum}</td>
                        <td class="factor-bar-cell">
                            <div class="factor-bar-bg">
                                <div class="factor-bar-fill" style="width:{fill_pct}%; background:{bar_color};"></div>
                            </div>
                        </td>
                        <td class="factor-status">
                            <span class="pill" style="background:{pill_bg}; color:{pill_color};">{pill_text}</span>
                        </td>
                    </tr>"""

    recs_html = ""
    for i, (title, detail) in enumerate(recommendations, 1):
        recs_html += f"""
                <div class="rec-item">
                    <div class="rec-number">{i}</div>
                    <div class="rec-content">
                        <div class="rec-title">{title}</div>
                        <div class="rec-detail">{detail}</div>
                    </div>
                </div>"""

    if not recs_html:
        recs_html = '<p class="no-recs">No critical issues found. Your profile is well-optimised.</p>'

    urgency_line = (
        "There are critical gaps costing you visibility in local search."
        if score < 50 else
        "There are improvements available that could increase your local search visibility."
    )

    name_encoded = name.replace(' ', '%20')

    # Build unsubscribe link — one-click webhook if recipient known, mailto fallback
    if recipient_email:
        unsub_url = f"http://43.134.33.213:8099/webhook/unsubscribe?email={quote(recipient_email)}"
    else:
        unsub_url = f"mailto:hello@raisemypresence.com?subject=Unsubscribe%20-{name_encoded}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Google Profile Audit — {name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #F9FAFB;
            color: #111827;
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
        }}
        .wrapper {{ max-width: 680px; margin: 0 auto; background: #FFFFFF; }}

        .header {{
            background: #111827;
            padding: 32px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .header-brand {{ font-size: 18px; font-weight: 700; color: #FFFFFF; letter-spacing: -0.02em; }}
        .header-brand span {{ color: #4ADE80; }}
        .header-meta {{ font-size: 12px; color: #6B7280; text-align: right; line-height: 1.6; }}

        .business-block {{ padding: 32px 40px 24px; border-bottom: 1px solid #F3F4F6; }}
        .report-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: #6B7280; margin-bottom: 8px; }}
        .business-name {{ font-size: 24px; font-weight: 700; color: #111827; margin-bottom: 4px; }}
        .business-address {{ font-size: 14px; color: #6B7280; }}
        .maps-link {{ display: inline-block; margin-top: 10px; font-size: 13px; color: #16A34A; text-decoration: none; font-weight: 500; }}

        .score-card {{
            margin: 28px 40px;
            border: 2px solid {color};
            border-radius: 16px;
            padding: 28px 32px;
            display: flex;
            align-items: center;
            gap: 28px;
            background: #FAFAFA;
        }}
        .score-circle {{
            width: 96px; height: 96px; border-radius: 50%;
            border: 6px solid {color};
            display: flex; flex-direction: column;
            align-items: center; justify-content: center; flex-shrink: 0;
        }}
        .score-number {{ font-size: 28px; font-weight: 800; color: {color}; line-height: 1; }}
        .score-max {{ font-size: 11px; color: #9CA3AF; font-weight: 500; }}
        .score-label-text {{ font-size: 20px; font-weight: 700; color: {color}; margin-bottom: 6px; }}
        .score-description {{ font-size: 14px; color: #6B7280; line-height: 1.5; }}

        .scoring-note {{
            margin: 0 40px 24px;
            padding: 12px 16px;
            background: #F9FAFB;
            border-left: 3px solid #D1D5DB;
            border-radius: 4px;
            font-size: 12px; color: #6B7280; line-height: 1.5;
        }}

        .section {{ padding: 0 40px 32px; }}
        .section-title {{
            font-size: 13px; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.06em; color: #374151;
            margin-bottom: 16px; padding-bottom: 10px;
            border-bottom: 1px solid #F3F4F6;
        }}

        table.breakdown {{ width: 100%; border-collapse: collapse; }}
        table.breakdown tr {{ border-bottom: 1px solid #F9FAFB; }}
        table.breakdown tr:last-child {{ border-bottom: none; }}
        .factor-name {{ font-size: 14px; color: #374151; padding: 10px 0; width: 38%; }}
        .factor-score {{ font-size: 12px; color: #9CA3AF; width: 14%; text-align: center; white-space: nowrap; }}
        .factor-bar-cell {{ width: 30%; padding: 0 12px; }}
        .factor-bar-bg {{ background: #F3F4F6; border-radius: 100px; height: 6px; overflow: hidden; }}
        .factor-bar-fill {{ height: 6px; border-radius: 100px; }}
        .factor-status {{ width: 18%; text-align: right; }}
        .pill {{ display: inline-block; font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 100px; white-space: nowrap; }}

        .rec-item {{ display: flex; gap: 16px; padding: 14px 0; border-bottom: 1px solid #F9FAFB; }}
        .rec-item:last-child {{ border-bottom: none; }}
        .rec-number {{
            width: 28px; height: 28px; border-radius: 50%;
            background: #111827; color: #FFFFFF;
            font-size: 13px; font-weight: 700;
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0; margin-top: 2px;
        }}
        .rec-title {{ font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 4px; }}
        .rec-detail {{ font-size: 13px; color: #6B7280; line-height: 1.5; }}
        .no-recs {{ font-size: 14px; color: #6B7280; padding: 12px 0; }}

        .cta-block {{ background: #F0FDF4; border-top: 2px solid #DCFCE7; padding: 32px 40px; text-align: center; }}
        .cta-block h3 {{ font-size: 18px; font-weight: 700; color: #111827; margin-bottom: 8px; }}
        .cta-block p {{ font-size: 14px; color: #6B7280; margin-bottom: 20px; max-width: 440px; margin-left: auto; margin-right: auto; }}
        .cta-btn {{ display: inline-block; background: #16A34A; color: #FFFFFF; font-size: 14px; font-weight: 600; padding: 12px 28px; border-radius: 8px; text-decoration: none; }}

        .footer {{ padding: 20px 40px; text-align: center; font-size: 12px; color: #9CA3AF; border-top: 1px solid #F3F4F6; line-height: 1.8; }}
        .footer a {{ color: #9CA3AF; }}
    </style>
</head>
<body>
<div class="wrapper">

    <div class="header">
        <div class="header-brand">Raise My <span>Presence</span></div>
        <div class="header-meta">Google Profile Audit<br>{report_date}</div>
    </div>

    <div class="business-block">
        <div class="report-label">Audit prepared for</div>
        <div class="business-name">{name}</div>
        <div class="business-address">{address}</div>
        <a href="{google_maps_url}" class="maps-link" target="_blank">View on Google Maps &rarr;</a>
    </div>

    <div class="score-card">
        <div class="score-circle">
            <div class="score-number">{score}</div>
            <div class="score-max">/ 100</div>
        </div>
        <div class="score-info">
            <div class="score-label-text">{label}</div>
            <div class="score-description">
                Your Google Business Profile scored <strong>{score}/100</strong>
                on our completeness audit. {urgency_line}
            </div>
        </div>
    </div>

    <div class="scoring-note">
        Score weights are based on the Whitespark 2026 Local Search Ranking Factors report
        (47 local SEO experts, 187 factors) and Google's official GBP documentation.
        Factors are weighted by their known contribution to Local Pack ranking.
    </div>

    <div class="section">
        <div class="section-title">Score Breakdown</div>
        <table class="breakdown">
            <tbody>{rows_html}
            </tbody>
        </table>
    </div>

    <div class="section">
        <div class="section-title">Priority Fixes &mdash; Highest Impact First</div>
        {recs_html}
    </div>

    <div class="cta-block">
        <h3>Want this fixed for you?</h3>
        <p>We handle everything &mdash; optimised description, Google Posts, photo guidelines,
        and ongoing management. No calls, no meetings.</p>
        <a href="mailto:hello@raisemypresence.com?subject=Fix%20my%20Google%20profile%20-%20{name_encoded}"
           class="cta-btn">Get Started &rarr;</a>
    </div>

    <div class="footer">
        &copy; 2026 Raise My Presence &middot;
        <a href="mailto:hello@raisemypresence.com">hello@raisemypresence.com</a><br>
        You received this because your business was identified in a public Google Maps audit.<br>
        <a href="{unsub_url}">Unsubscribe</a>
    </div>

</div>
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
        "address": "123 Main St, Gold Coast QLD 4217",
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

    html = generate_report(sample)
    expected_score = 0+8+7+15+0+6+8+3
    actual_score = recompute_score(sample["score_breakdown"])
    assert actual_score == expected_score
    assert "999" not in html
    assert str(actual_score) in html

    with open("/tmp/sample_report_v2.html", "w") as f:
        f.write(html)

    print(f"Score recomputed correctly: {actual_score}/100")
    print(f"Report generated: {len(html):,} chars")
    print("Preview: /tmp/sample_report_v2.html")
