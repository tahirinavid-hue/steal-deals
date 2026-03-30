"""
Module B: WNY 3-Row EV Leases — Tier 2 (LLM-powered)
- Weekly cron
- Dynamically discovers WNY dealerships for Kia EV9 and Hyundai Ioniq 9
- Uses LLM to calculate "True $0 Down" monthly cost from raw page text
- Also applies hardware fallback logic for unlisted / equivalent 3-row EVs
- Alert if True $0 Down Monthly Cost < $650/month
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bs4 import BeautifulSoup
from shared.proxy import fetch_page, google_search_urls
from shared.db import is_seen, mark_seen, record_heartbeat
from shared.email import send_deal_alert, send_heartbeat
from shared.llm import extract_structured, strip_html

MODULE = "ev_leases"
ALERT_THRESHOLD = float(os.environ.get("EV_LEASE_THRESHOLD", "650"))
ERIE_TAX = 0.0875

# ── Target vehicles ──────────────────────────────────────────────────────────

FAST_PATH_TARGETS = [
    {"make": "Kia", "model": "EV9", "trims": ["Land AWD"], "search": "Kia EV9 Land AWD lease WNY western new york"},
    {"make": "Hyundai", "model": "Ioniq 9", "trims": ["Limited", "Calligraphy", "Calligraphy Design"],
     "search": "Hyundai Ioniq 9 Limited Calligraphy lease WNY western new york"},
]

FALLBACK_SEARCH_QUERIES = [
    "3-row EV lease WNY western new york dealership 2024 2025",
    "electric SUV 3 row lease buffalo ny rochester ny",
    "Rivian R1S lease western new york",
    "Mercedes EQB lease buffalo ny",
    "Volkswagen ID.Buzz lease WNY",
]

DEALER_DISCOVERY_QUERIES = [
    "Kia dealerships western new york buffalo",
    "Hyundai dealerships WNY buffalo amherst",
    "EV dealerships western new york lease specials",
]

# ── LLM prompts ──────────────────────────────────────────────────────────────

FAST_PATH_PROMPT = """
You are extracting lease deal information from a car dealership webpage.
Extract the following fields and return them as a JSON object:

{
  "vehicle_title": "Full vehicle name as listed",
  "trim": "Trim level",
  "monthly_payment": <number or null>,
  "lease_term_months": <number or null>,
  "due_at_signing": <number or null>,
  "cap_cost_reduction": <number or null>,
  "other_hidden_fees": <number or null>,
  "availability": "in_stock | incoming | pre_order | unknown",
  "listing_found": true|false,
  "notes": "Any important fine print in one sentence"
}

If no lease deal is visible for this vehicle, set listing_found to false.
Return ONLY the JSON object, no explanation.
"""

FALLBACK_PROMPT = """
You are evaluating whether a vehicle listed on a dealership page qualifies as a 3-row EV alternative.

A qualifying vehicle MUST have ALL of the following:
1. AWD powertrain
2. Second-row captain's chairs (NOT a bench seat)
3. Minimum EPA-rated battery range of 280 miles

Also extract the lease terms if present.

Return a JSON object:
{
  "qualifies": true|false,
  "reason": "one-sentence explanation",
  "vehicle_title": "full vehicle name",
  "trim": "trim level",
  "awd": true|false,
  "captains_chairs": true|false,
  "range_miles": <number or null>,
  "monthly_payment": <number or null>,
  "lease_term_months": <number or null>,
  "due_at_signing": <number or null>,
  "cap_cost_reduction": <number or null>,
  "other_hidden_fees": <number or null>,
  "availability": "in_stock | incoming | pre_order | unknown",
  "notes": "fine print summary"
}

Return ONLY the JSON object.
"""


# ── Finance engine ────────────────────────────────────────────────────────────

def calc_true_monthly(data: dict) -> float | None:
    """
    Apply the True $0 Down engine from the PRD.
    Returns the true monthly cost or None if data is insufficient.
    """
    monthly = data.get("monthly_payment")
    term = data.get("lease_term_months")
    if not monthly or not term:
        return None

    hidden_down = sum(filter(None, [
        data.get("due_at_signing") or 0,
        data.get("cap_cost_reduction") or 0,
        data.get("other_hidden_fees") or 0,
    ]))

    total_base = (monthly * term) + hidden_down
    total_tax = total_base * ERIE_TAX
    true_monthly = (total_base + total_tax) / term
    return round(true_monthly, 2)


def _format_fine_print(data: dict, true_monthly: float) -> str:
    term = data.get("lease_term_months", "?")
    hidden = sum(filter(None, [
        data.get("due_at_signing") or 0,
        data.get("cap_cost_reduction") or 0,
        data.get("other_hidden_fees") or 0,
    ]))
    return (
        f"Advertised ${data.get('monthly_payment', '?')}/mo × {term} months"
        f"{f' + ${hidden:.0f} hidden at signing' if hidden else ''}"
        f" → true $0-down cost ${true_monthly:.0f}/mo after 8.75% Erie County tax."
    )


# ── Discovery ────────────────────────────────────────────────────────────────

def discover_dealer_urls() -> list[str]:
    """Discover WNY dealership pages via search."""
    all_urls = []
    seen = set()
    for q in DEALER_DISCOVERY_QUERIES:
        try:
            urls = google_search_urls(q, num=8)
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    all_urls.append(u)
        except Exception as e:
            print(f"[{MODULE}] Discovery search error '{q}': {e}")
    return all_urls


def get_target_urls() -> list[dict]:
    """
    Return list of {url, mode, vehicle_hint} dicts.
    mode = 'fast_path' or 'fallback'
    """
    results = []
    seen = set()

    # Fast-path searches
    for target in FAST_PATH_TARGETS:
        try:
            urls = google_search_urls(target["search"], num=8)
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    results.append({"url": u, "mode": "fast_path", "target": target})
        except Exception as e:
            print(f"[{MODULE}] Fast-path search error: {e}")

    # Dealer homepages for fallback crawl
    dealer_urls = discover_dealer_urls()
    for u in dealer_urls:
        if u not in seen:
            seen.add(u)
            results.append({"url": u, "mode": "fallback", "target": None})

    # Fallback EV searches
    for q in FALLBACK_SEARCH_QUERIES:
        try:
            urls = google_search_urls(q, num=6)
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    results.append({"url": u, "mode": "fallback", "target": None})
        except Exception as e:
            print(f"[{MODULE}] Fallback search error: {e}")

    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    print(f"[{MODULE}] Starting scan. Alert threshold: <${ALERT_THRESHOLD}/mo")
    deals_found = []

    skip_domains = ["youtube.com", "reddit.com", "facebook.com", "twitter.com",
                    "instagram.com", "yelp.com", "bbb.org", "edmunds.com",
                    "cars.com", "carmax.com", "carvana.com"]

    candidates = get_target_urls()
    print(f"[{MODULE}] Evaluating {len(candidates)} candidate URLs")

    for item in candidates:
        url = item["url"]
        mode = item["mode"]
        target = item["target"]

        if any(d in url for d in skip_domains):
            continue

        try:
            print(f"[{MODULE}] [{mode}] Fetching: {url}")
            html = fetch_page(url, use_js=True)
        except Exception as e:
            print(f"[{MODULE}] Fetch error {url}: {e}")
            continue

        page_text = strip_html(html)

        # Quick relevance check before spending LLM tokens
        ev_keywords = ["ev9", "ioniq", "electric", "ev ", "lease", "monthly", "awd"]
        if not any(kw in page_text.lower() for kw in ev_keywords):
            continue

        # Choose prompt
        if mode == "fast_path":
            prompt = FAST_PATH_PROMPT
        else:
            prompt = FALLBACK_PROMPT

        try:
            data = extract_structured(prompt, page_text)
        except Exception as e:
            print(f"[{MODULE}] LLM error {url}: {e}")
            continue

        if data.get("_parse_error"):
            print(f"[{MODULE}] LLM parse error at {url}")
            continue

        # Fast-path: check listing_found
        if mode == "fast_path" and not data.get("listing_found", False):
            continue

        # Fallback: check qualification
        if mode == "fallback" and not data.get("qualifies", False):
            print(f"[{MODULE}] Fallback not qualified: {data.get('reason', '')}")
            continue

        true_monthly = calc_true_monthly(data)
        if true_monthly is None:
            print(f"[{MODULE}] Could not calculate true monthly cost at {url}")
            continue

        vehicle_title = data.get("vehicle_title", "Unknown EV")
        trim = data.get("trim", "")
        availability = data.get("availability", "unknown")

        print(f"[{MODULE}] {vehicle_title} {trim} → true ${true_monthly}/mo | avail={availability}")

        if true_monthly >= ALERT_THRESHOLD:
            continue

        deal_id = f"{url}|{vehicle_title}|{trim}|{true_monthly:.0f}"
        if is_seen(MODULE, deal_id):
            print(f"[{MODULE}] Already alerted: {deal_id}")
            continue

        avail_label = {
            "in_stock": "In Stock",
            "incoming": "Incoming / Ordered",
            "pre_order": "Pre-Order",
        }.get(availability, "Status Unknown")

        deals_found.append({
            "title": f"{vehicle_title} {trim} ({avail_label})",
            "true_cost": f"${true_monthly:.0f}/mo true $0-down",
            "advertised": f"${data.get('monthly_payment', '?')}/mo advertised",
            "url": url,
            "fine_print": _format_fine_print(data, true_monthly),
            "_deal_id": deal_id,
        })

    if deals_found:
        best = min(deals_found, key=lambda d: float(d["true_cost"].replace("$", "").replace("/mo true $0-down", "")))
        subject = f"DEAL ALERT: {best['title'].split('(')[0].strip()} for {best['true_cost'].split(' ')[0]}/mo"
        rows = [{k: v for k, v in d.items() if not k.startswith("_")} for d in deals_found]
        send_deal_alert(subject, rows)
        for d in deals_found:
            mark_seen(MODULE, d["_deal_id"])
        print(f"[{MODULE}] Alerted {len(deals_found)} deal(s).")
    else:
        print(f"[{MODULE}] No qualifying leases found.")
        send_heartbeat(MODULE, "No qualifying leases found",
                       f"Checked {len(candidates)} pages. Threshold: <${ALERT_THRESHOLD}/mo true $0-down")

    record_heartbeat(MODULE)


if __name__ == "__main__":
    run()
