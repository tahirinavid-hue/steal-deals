"""
Module C: WNY Solar "Fire Sale" & PPA Arbitrage — Tier 2 (LLM-powered)
- Weekly cron
- Dynamically discovers local WNY solar installers and national PPA providers
- Condition 1: Liquidation/overstock events — alert if gross $/W < $2.00/W
- Condition 2: PPA loophole — alert if $0-down, escalator < 2.9%, rate < $0.12/kWh
- Combined email if both conditions trigger simultaneously
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.proxy import fetch_page, google_search_urls
from shared.db import is_seen, mark_seen, record_heartbeat
from shared.email import send_deal_alert, send_heartbeat
from shared.llm import extract_structured, strip_html

MODULE = "solar"

# Thresholds
PPW_THRESHOLD = float(os.environ.get("SOLAR_PPW_THRESHOLD", "2.00"))   # $/W gross
PPA_RATE_CEILING = float(os.environ.get("SOLAR_PPA_CEILING", "0.12"))  # $/kWh
PPA_ESCALATOR_MAX = float(os.environ.get("SOLAR_ESCALATOR_MAX", "2.9"))  # %

# ── Search queries ────────────────────────────────────────────────────────────

DISCOVERY_QUERIES = [
    "solar panel installation WNY western new york grand island buffalo clearance",
    "solar installer buffalo ny special offer promotion",
    "solar panel overstock liquidation western new york",
    "solar PPA power purchase agreement buffalo rochester ny",
    "sunrun sunnova solar lease WNY new york",
    "solar energy grand island ny installer quote",
    "residential solar promotion deal buffalo amherst ny",
    "solar panel fire sale western new york 2025 2026",
    "solar PPA zero down western new york rate per kwh",
    "local solar company buffalo ny financing promotion",
]

# ── LLM prompts ──────────────────────────────────────────────────────────────

CONDITION1_PROMPT = """
You are analyzing a solar installer or marketplace webpage for a potential inventory liquidation event.

Extract the following and return as JSON:

{
  "is_liquidation_event": true|false,
  "liquidation_evidence": "quote or phrase from page indicating clearance/overstock/fire sale, or null",
  "gross_system_cost_usd": <number or null>,
  "system_size_kw": <number or null>,
  "price_per_watt_gross": <number or null>,
  "includes_installation": true|false|null,
  "notes": "one-sentence summary of the offer"
}

A liquidation event means the page explicitly mentions: clearance, overstock, fire sale, end-of-season,
discontinued inventory, limited stock, or similar language.

If gross_system_cost_usd and system_size_kw are both present but price_per_watt_gross is null,
calculate it as: gross_system_cost_usd / (system_size_kw * 1000).

Return ONLY the JSON object.
"""

CONDITION2_PROMPT = """
You are analyzing a solar PPA (Power Purchase Agreement) or lease offer on a webpage.

Extract the following and return as JSON:

{
  "is_ppa_or_lease": true|false,
  "zero_down": true|false,
  "starting_rate_per_kwh": <number or null>,
  "annual_escalator_pct": <number or null>,
  "contract_term_years": <number or null>,
  "buyout_option": true|false|null,
  "notes": "one-sentence summary of terms and any fine print"
}

A true $0-down structure means no upfront payment, deposit, or activation fee is required.
The starting rate is the initial $/kWh rate charged under the PPA.
The annual escalator is the yearly percentage increase in the rate.

Return ONLY the JSON object.
"""


# ── Evaluation logic ──────────────────────────────────────────────────────────

def evaluate_condition1(data: dict) -> tuple[bool, float | None]:
    """Return (qualifies, price_per_watt)."""
    if not data.get("is_liquidation_event"):
        return False, None

    ppw = data.get("price_per_watt_gross")
    if not ppw:
        cost = data.get("gross_system_cost_usd")
        size_kw = data.get("system_size_kw")
        if cost and size_kw:
            ppw = round(cost / (size_kw * 1000), 3)

    if ppw and ppw < PPW_THRESHOLD:
        return True, ppw
    return False, ppw


def evaluate_condition2(data: dict) -> tuple[bool, float | None]:
    """Return (qualifies, starting_rate)."""
    if not data.get("is_ppa_or_lease"):
        return False, None
    if not data.get("zero_down"):
        return False, None

    rate = data.get("starting_rate_per_kwh")
    escalator = data.get("annual_escalator_pct")

    if rate is None:
        return False, None

    escalator_ok = (escalator is None) or (escalator < PPA_ESCALATOR_MAX)
    rate_ok = rate < PPA_RATE_CEILING

    if rate_ok and escalator_ok:
        return True, rate
    return False, rate


# ── Discovery ────────────────────────────────────────────────────────────────

def get_candidate_urls() -> list[str]:
    seen = set()
    results = []
    for q in DISCOVERY_QUERIES:
        try:
            urls = google_search_urls(q, num=6)
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    results.append(u)
        except Exception as e:
            print(f"[{MODULE}] Search error '{q}': {e}")
    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    print(f"[{MODULE}] Starting scan. PPW threshold: <${PPW_THRESHOLD}/W | PPA ceiling: <${PPA_RATE_CEILING}/kWh")

    skip_domains = ["youtube.com", "reddit.com", "facebook.com", "twitter.com",
                    "instagram.com", "yelp.com", "bbb.org", "energysage.com/news",
                    "forbes.com", "cnet.com"]

    candidates = get_candidate_urls()
    print(f"[{MODULE}] Evaluating {len(candidates)} candidate URLs")

    deals_found = []

    for url in candidates:
        if any(d in url for d in skip_domains):
            continue

        try:
            print(f"[{MODULE}] Fetching: {url}")
            html = fetch_page(url, use_js=True)
        except Exception as e:
            print(f"[{MODULE}] Fetch error {url}: {e}")
            continue

        page_text = strip_html(html)

        # Quick relevance check
        solar_kw = ["solar", "photovoltaic", "pv ", "ppa", "power purchase", "kwh", "panel"]
        if not any(kw in page_text.lower() for kw in solar_kw):
            continue

        # Evaluate both conditions — run both LLM calls (they're cheap with Flash)
        c1_data, c2_data = None, None
        c1_qualifies, ppw = False, None
        c2_qualifies, rate = False, None

        # Condition 1: only if liquidation language seems present
        liquidation_hints = ["clearance", "overstock", "fire sale", "liquidat", "limited stock",
                             "end of season", "discontinued", "closeout"]
        if any(h in page_text.lower() for h in liquidation_hints):
            try:
                c1_data = extract_structured(CONDITION1_PROMPT, page_text)
                if not c1_data.get("_parse_error"):
                    c1_qualifies, ppw = evaluate_condition1(c1_data)
            except Exception as e:
                print(f"[{MODULE}] C1 LLM error {url}: {e}")

        # Condition 2: only if PPA/lease language is present
        ppa_hints = ["ppa", "power purchase", "lease", "per kwh", "$/kwh", "escalat", "zero down", "$0 down"]
        if any(h in page_text.lower() for h in ppa_hints):
            try:
                c2_data = extract_structured(CONDITION2_PROMPT, page_text)
                if not c2_data.get("_parse_error"):
                    c2_qualifies, rate = evaluate_condition2(c2_data)
            except Exception as e:
                print(f"[{MODULE}] C2 LLM error {url}: {e}")

        if not c1_qualifies and not c2_qualifies:
            continue

        # Build deal entry
        deal_parts = []

        if c1_qualifies and ppw:
            print(f"[{MODULE}] C1 MATCH: ${ppw:.3f}/W at {url}")
            deal_parts.append(("liquidation", f"${ppw:.2f}/W gross",
                               f"MSRP-level pricing vs clearance ${ppw:.2f}/W",
                               c1_data.get("notes", "Liquidation event detected.")))

        if c2_qualifies and rate:
            escalator = (c2_data or {}).get("annual_escalator_pct")
            escalator_str = f"{escalator:.1f}%" if escalator else "undisclosed"
            print(f"[{MODULE}] C2 MATCH: ${rate:.3f}/kWh at {url}")
            deal_parts.append(("ppa", f"${rate:.3f}/kWh locked",
                               "$0 down PPA",
                               f"$0-down PPA at ${rate:.3f}/kWh with {escalator_str}/yr escalator. "
                               f"{(c2_data or {}).get('notes', '')}"))

        # Dedup key
        deal_id = "|".join([url] + [p[0] for p in deal_parts])
        if is_seen(MODULE, deal_id):
            print(f"[{MODULE}] Already alerted: {deal_id}")
            continue

        for dtype, true_cost, advertised, fine_print in deal_parts:
            if dtype == "liquidation":
                title = f"Solar Liquidation — WNY Installer"
                if c1_data:
                    size = c1_data.get("system_size_kw")
                    title = f"Solar Clearance — {f'{size}kW system' if size else 'WNY Installer'}"
            else:
                title = "Solar PPA Loophole — $0 Down"
                if c2_data:
                    term = c2_data.get("contract_term_years")
                    title = f"Solar PPA $0-Down — {f'{term}-year contract' if term else 'WNY'}"

            deals_found.append({
                "title": title,
                "true_cost": true_cost,
                "advertised": advertised,
                "url": url,
                "fine_print": fine_print,
                "_deal_id": deal_id,
                "_type": dtype,
            })

    if deals_found:
        # Build subject — prefer the most impactful deal
        liquidation = [d for d in deals_found if d["_type"] == "liquidation"]
        ppa = [d for d in deals_found if d["_type"] == "ppa"]

        if liquidation and ppa:
            subject = "DEAL ALERT: Solar Fire Sale + PPA Loophole — WNY"
        elif liquidation:
            best = min(liquidation, key=lambda d: float(d["true_cost"].replace("$", "").replace("/W gross", "")))
            subject = f"DEAL ALERT: Solar Clearance at {best['true_cost']} — WNY"
        else:
            best = min(ppa, key=lambda d: float(d["true_cost"].replace("$", "").replace("/kWh locked", "")))
            subject = f"DEAL ALERT: Solar PPA at {best['true_cost']} — WNY"

        rows = [{k: v for k, v in d.items() if not k.startswith("_")} for d in deals_found]
        send_deal_alert(subject, rows)
        seen_ids = set()
        for d in deals_found:
            if d["_deal_id"] not in seen_ids:
                mark_seen(MODULE, d["_deal_id"])
                seen_ids.add(d["_deal_id"])
        print(f"[{MODULE}] Alerted {len(deals_found)} deal(s).")
    else:
        print(f"[{MODULE}] No qualifying solar deals found.")
        send_heartbeat(MODULE, "No qualifying solar deals found",
                       f"Checked {len(candidates)} pages. "
                       f"Thresholds: <${PPW_THRESHOLD}/W gross or <${PPA_RATE_CEILING}/kWh PPA")

    record_heartbeat(MODULE)


if __name__ == "__main__":
    run()
