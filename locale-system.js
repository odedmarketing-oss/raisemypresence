/**
 * Raise My Presence — Locale System
 * Swaps text content based on visitor country.
 * Country code injected by Cloudflare Worker via window.__RMP_COUNTRY
 * or overridden via ?country=XX query param for testing.
 *
 * Created: April 10, 2026 (Day 5)
 */

(function () {
  'use strict';

  // ── Locale Definitions ──────────────────────────────────────────────
  var locales = {

    US: {
      // Pricing
      kit_price: '$79',
      monthly_price: '$39/mo',
      kit_price_range: '$79',
      monthly_price_range: '$39',
      currency_label: 'USD',
      price_qualifier_kit: 'USD · one-time',
      price_qualifier_monthly: 'USD/mo',

      // Spelling
      optimization_word: 'Optimization',
      optimization_word_lower: 'optimization',
      optimized_word: 'optimized',
      optimize_word: 'optimize',
      personalized_word: 'personalized',
      customized_word: 'customized',
      maximizing_word: 'maximizing',

      // Geography
      example_city_1: 'Austin',
      example_city_2: 'Denver',
      example_city_3: 'Portland',
      example_search: '"plumber near me"',
      example_audit_business: "Joe's Plumbing, Austin",

      // Trust & compliance
      trust_region_label: 'Serving local businesses across the United States',
      hero_badge: 'Google Maps Optimization for Local Businesses',
      compliance_note: 'CAN-SPAM compliant',

      // CTA mailto subjects
      cta_audit_subject: 'Free%20Audit%20Request',
      cta_kit_subject: 'Optimization%20Kit%20Inquiry',
      cta_monthly_subject: 'Monthly%20Management%20Inquiry',

      // Tier labels
      tier_kit_heading: 'Optimization Kit',
      tier_kit_price_display: '$79',
      tier_kit_price_qualifier: 'USD',
      tier_monthly_price_display: '$39',
      tier_monthly_price_qualifier: 'USD/mo',
      tier_kit_cta: 'Get the Kit'
    },

    GB: {
      kit_price: '£59',
      monthly_price: '£29/mo',
      kit_price_range: '£59',
      monthly_price_range: '£29',
      currency_label: 'GBP',
      price_qualifier_kit: 'GBP · one-time',
      price_qualifier_monthly: 'GBP/mo',

      optimization_word: 'Optimisation',
      optimization_word_lower: 'optimisation',
      optimized_word: 'optimised',
      optimize_word: 'optimise',
      personalized_word: 'personalised',
      customized_word: 'customised',
      maximizing_word: 'maximising',

      example_city_1: 'Bristol',
      example_city_2: 'Leeds',
      example_city_3: 'Manchester',
      example_search: '"plumber near me"',
      example_audit_business: "Smith's Dental, Bristol",

      trust_region_label: 'Serving local businesses across the United Kingdom',
      hero_badge: 'Google Maps Optimisation for Local Businesses',
      compliance_note: 'GDPR & PECR compliant',

      cta_audit_subject: 'Free%20Audit%20Request',
      cta_kit_subject: 'Optimisation%20Kit%20Enquiry',
      cta_monthly_subject: 'Monthly%20Management%20Enquiry',

      tier_kit_heading: 'Optimisation Kit',
      tier_kit_price_display: '£59',
      tier_kit_price_qualifier: 'GBP',
      tier_monthly_price_display: '£29',
      tier_monthly_price_qualifier: 'GBP/mo',
      tier_kit_cta: 'Get the Kit'
    },

    AU: {
      kit_price: '$99',
      monthly_price: '$49/mo',
      kit_price_range: '$99',
      monthly_price_range: '$49',
      currency_label: 'AUD',
      price_qualifier_kit: 'AUD · one-time',
      price_qualifier_monthly: 'AUD/mo',

      optimization_word: 'Optimisation',
      optimization_word_lower: 'optimisation',
      optimized_word: 'optimised',
      optimize_word: 'optimise',
      personalized_word: 'personalised',
      customized_word: 'customised',
      maximizing_word: 'maximising',

      example_city_1: 'Gold Coast',
      example_city_2: 'Geelong',
      example_city_3: 'Toowoomba',
      example_search: '"dentist near me"',
      example_audit_business: "Smith's Dental, Gold Coast",

      trust_region_label: 'Serving regional businesses across Australia & New Zealand',
      hero_badge: 'Google Maps Optimisation for AU & NZ',
      compliance_note: 'Spam Act 2003 compliant',

      cta_audit_subject: 'Free%20Audit%20Request',
      cta_kit_subject: 'Optimisation%20Kit%20Enquiry',
      cta_monthly_subject: 'Monthly%20Management%20Enquiry',

      tier_kit_heading: 'Optimisation Kit',
      tier_kit_price_display: '$99',
      tier_kit_price_qualifier: 'AUD',
      tier_monthly_price_display: '$49',
      tier_monthly_price_qualifier: 'AUD/mo',
      tier_kit_cta: 'Get the Kit'
    },

    NZ: {
      kit_price: '$99',
      monthly_price: '$49/mo',
      kit_price_range: '$99',
      monthly_price_range: '$49',
      currency_label: 'NZD',
      price_qualifier_kit: 'NZD · one-time',
      price_qualifier_monthly: 'NZD/mo',

      optimization_word: 'Optimisation',
      optimization_word_lower: 'optimisation',
      optimized_word: 'optimised',
      optimize_word: 'optimise',
      personalized_word: 'personalised',
      customized_word: 'customised',
      maximizing_word: 'maximising',

      example_city_1: 'Hamilton',
      example_city_2: 'Tauranga',
      example_city_3: 'Dunedin',
      example_search: '"dentist near me"',
      example_audit_business: "Smith's Dental, Hamilton",

      trust_region_label: 'Serving local businesses across Australia & New Zealand',
      hero_badge: 'Google Maps Optimisation for AU & NZ',
      compliance_note: 'NZ Unsolicited Electronic Messages Act compliant',

      cta_audit_subject: 'Free%20Audit%20Request',
      cta_kit_subject: 'Optimisation%20Kit%20Enquiry',
      cta_monthly_subject: 'Monthly%20Management%20Enquiry',

      tier_kit_heading: 'Optimisation Kit',
      tier_kit_price_display: '$99',
      tier_kit_price_qualifier: 'NZD',
      tier_monthly_price_display: '$49',
      tier_monthly_price_qualifier: 'NZD/mo',
      tier_kit_cta: 'Get the Kit'
    }
  };

  // ── Country → Locale Mapping ────────────────────────────────────────
  var countryMap = {
    US: 'US', CA: 'US', PR: 'US',
    GB: 'GB', IE: 'GB',
    AU: 'AU',
    NZ: 'NZ'
  };

  // ── Detect Country ──────────────────────────────────────────────────
  function detectCountry() {
    // Priority 1: Cloudflare Worker injection
    if (window.__RMP_COUNTRY) {
      return window.__RMP_COUNTRY;
    }
    // Priority 2: URL query param (testing)
    var params = new URLSearchParams(window.location.search);
    var qc = params.get('country');
    if (qc) {
      return qc.toUpperCase();
    }
    // Default
    return 'US';
  }

  // ── Resolve Locale ──────────────────────────────────────────────────
  function resolveLocale(countryCode) {
    var mapped = countryMap[countryCode];
    if (mapped && locales[mapped]) {
      return locales[mapped];
    }
    return locales['US']; // fallback
  }

  // ── Apply Locale ────────────────────────────────────────────────────
  function applyLocale() {
    var country = detectCountry();
    var locale = resolveLocale(country);

    // Swap text content
    var textEls = document.querySelectorAll('[data-locale-key]');
    for (var i = 0; i < textEls.length; i++) {
      var key = textEls[i].getAttribute('data-locale-key');
      if (locale[key] !== undefined) {
        textEls[i].textContent = locale[key];
      }
    }

    // Swap mailto subjects in href attributes
    var hrefEls = document.querySelectorAll('[data-locale-href]');
    for (var j = 0; j < hrefEls.length; j++) {
      var hrefKey = hrefEls[j].getAttribute('data-locale-href');
      if (locale[hrefKey] !== undefined) {
        var currentHref = hrefEls[j].getAttribute('href');
        // Replace subject parameter in mailto link
        var newHref = currentHref.replace(/subject=[^&]*/, 'subject=' + locale[hrefKey]);
        hrefEls[j].setAttribute('href', newHref);
      }
    }
  }

  // ── Run ─────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyLocale);
  } else {
    applyLocale();
  }

})();
