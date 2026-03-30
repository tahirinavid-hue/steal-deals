"""
Module A: EGO SNT 2416 Snowblower — Tier 1 (NO LLM)
- Hourly cron
- Dynamic search engine discovery of retailer product pages
- Programmatic price/stock extraction only
- Alert if price <= PRICE_THRESHOLD or LLM-free "steal" heuristic triggers
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bs4 import BeautifulSoup
from shared.proxy import fetch_page, google_search_urls
from shared.db import is_seen, mark_seen, record_heartbeat
from shared.email import send_deal_alert, send_heartbeat

# ── Config ──────────────────────────────────────────────────────────────────
PRODUCT_NAME = "EGO SNT2416 snowblower"
PRICE_THRESHOLD = float(os.environ.get("SNOWBLOWER_THRESHOLD", "1600"))
# Any price this far below MSRP is automatically a "steal" regardless of threshold
STEAL_DISCOUNT_PCT = float(os.environ.get("SNOWBLOWER_STEAL_PCT", "20"))
MSRP = float(os.environ.get("SNOWBLOWER_MSRP", "1999"))

SEARCH_QUERIES = [
    "EGO SNT2416 snowblower buy price",
    "EGO SNT2416 two-stage snowblower in stock",
    'EGO Power+ "SNT2416" price dealer',
    "EGO SNT2416 Home Depot Lowes price",
]

MODULE = "snowblower"


# ── Parser helpers ───────────────────────────────────────────────────────────

def _parse_homedepot(html: str) -> dict | None:
    """Extract price and stock from a Home Depot PDP."""
    soup = BeautifulSoup(html, "html.parser")

    # Price: <div class="price-format__main-price"> or structured JSON-LD
    price = None
    # Try JSON-LD first (most reliable)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string)
            if isinstance(data, list):
                data = data[0]
            offers = data.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0]
            price_str = offers.get("price") or offers.get("lowPrice")
            if price_str:
                price = float(str(price_str).replace(",", ""))
                break
        except Exception:
            continue

    # Fallback: regex on raw HTML
    if not price:
        m = re.search(r'"price"\s*:\s*"?([\d,]+\.?\d*)"?', html)
        if m:
            price = float(m.group(1).replace(",", ""))

    # Stock: look for "In Stock" text or availability schema
    in_stock = bool(re.search(r'"availability"\s*:\s*"[^"]*InStock[^"]*"', html, re.I))
    if not in_stock:
        in_stock = bool(soup.find(string=re.compile(r"\bin stock\b", re.I)))

    if price:
        return {"price": price, "in_stock": in_stock}
    return None


def _parse_lowes(html: str) -> dict | None:
    """Extract price and stock from a Lowe's PDP."""
    soup = BeautifulSoup(html, "html.parser")
    price = None

    # Lowe's embeds pricing in a <script id="__NEXT_DATA__"> JSON blob
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data:
        try:
            import json
            data = json.loads(next_data.string)
            # Drill into the deeply nested price structure
            props = data.get("props", {}).get("pageProps", {})
            product = props.get("product", {}) or props.get("initialData", {}).get("product", {})
            price_info = product.get("price", {})
            price = price_info.get("sellingPrice") or price_info.get("regularPrice")
            if price:
                price = float(price)
        except Exception:
            pass

    if not price:
        m = re.search(r'"sellingPrice"\s*:\s*([\d.]+)', html)
        if m:
            price = float(m.group(1))

    in_stock = bool(re.search(r'"availability"\s*:\s*"[^"]*InStock', html, re.I))
    if not in_stock:
        in_stock = bool(BeautifulSoup(html, "html.parser").find(string=re.compile(r"\bin stock\b", re.I)))

    if price:
        return {"price": price, "in_stock": in_stock}
    return None


def _parse_generic(html: str) -> dict | None:
    """Generic JSON-LD + regex price extraction for unknown retailers."""
    import json
    soup = BeautifulSoup(html, "html.parser")
    price = None

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                data = data[0]
            offers = data.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0]
            p = offers.get("price") or offers.get("lowPrice")
            if p:
                price = float(str(p).replace(",", ""))
                break
        except Exception:
            continue

    if not price:
        # Try meta og:price or itemprop=price
        tag = soup.find("meta", {"property": "product:price:amount"}) or \
              soup.find("meta", {"itemprop": "price"})
        if tag and tag.get("content"):
            try:
                price = float(tag["content"].replace(",", ""))
            except ValueError:
                pass

    if not price:
        m = re.search(r'\$\s*(1[,\d]*\d{3}(?:\.\d{2})?)', html)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    in_stock = bool(re.search(r'\bin[\s-]?stock\b', html, re.I))

    if price:
        return {"price": price, "in_stock": in_stock}
    return None


# Known retailer domains we trust for structured price extraction
# Defined after the parser functions to avoid NameError
RETAILER_PATTERNS = {
    "homedepot.com": _parse_homedepot,
    "lowes.com": _parse_lowes,
    "acehardware.com": _parse_generic,
    "truevalue.com": _parse_generic,
    "walmart.com": _parse_generic,
}


def _get_parser(url: str):
    for domain, fn in RETAILER_PATTERNS.items():
        if domain in url:
            return fn
    return _parse_generic


def _is_steal(price: float) -> bool:
    """True if price is below threshold OR >STEAL_DISCOUNT_PCT off MSRP."""
    if price <= PRICE_THRESHOLD:
        return True
    if MSRP and price <= MSRP * (1 - STEAL_DISCOUNT_PCT / 100):
        return True
    return False


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    print(f"[{MODULE}] Starting scan. Threshold=${PRICE_THRESHOLD}, MSRP=${MSRP}")
    deals_found = []
    seen_urls = set()

    # Gather candidate URLs from multiple search queries
    all_urls = []
    for q in SEARCH_QUERIES:
        try:
            urls = google_search_urls(q, num=6)
            all_urls.extend(urls)
        except Exception as e:
            print(f"[{MODULE}] Search failed for '{q}': {e}")

    # Deduplicate
    unique_urls = []
    for u in all_urls:
        if u not in seen_urls:
            seen_urls.add(u)
            unique_urls.append(u)

    print(f"[{MODULE}] Checking {len(unique_urls)} candidate URLs")

    for url in unique_urls:
        # Skip irrelevant pages (forum posts, YouTube, etc.)
        skip_domains = ["youtube.com", "reddit.com", "facebook.com", "twitter.com",
                        "instagram.com", "yelp.com", "bbb.org"]
        if any(d in url for d in skip_domains):
            continue

        # Confirm URL likely contains the right product
        if "snt2416" not in url.lower() and "snt-2416" not in url.lower() and "snowblower" not in url.lower():
            # Quick HEAD check to see if we should bother
            pass

        try:
            print(f"[{MODULE}] Fetching: {url}")
            html = fetch_page(url, use_js=True)
        except Exception as e:
            print(f"[{MODULE}] Fetch error {url}: {e}")
            continue

        # Verify this page is actually about the SNT2416
        if "snt2416" not in html.lower() and "snt-2416" not in html.lower() and "ego" not in html.lower():
            continue

        parser = _get_parser(url)
        try:
            result = parser(html)
        except Exception as e:
            print(f"[{MODULE}] Parse error {url}: {e}")
            continue

        if not result:
            continue

        price = result["price"]
        in_stock = result["in_stock"]

        print(f"[{MODULE}] {url} → ${price:.2f} | in_stock={in_stock}")

        if not _is_steal(price):
            continue

        # Build dedup key from URL + price (price changes break dedup intentionally)
        deal_id = f"{url}|{price:.0f}"
        if is_seen(MODULE, deal_id):
            print(f"[{MODULE}] Already alerted: {deal_id}")
            continue

        stock_note = "In Stock" if in_stock else "Stock status unknown"
        discount_pct = round((1 - price / MSRP) * 100) if MSRP else 0
        reason = (
            f"Price is ${price:.2f} — "
            f"{f'{discount_pct}% below MSRP (${MSRP:.0f})' if discount_pct else f'below threshold (${PRICE_THRESHOLD:.0f})'}. "
            f"{stock_note}."
        )

        deals_found.append({
            "title": f"EGO SNT2416 Two-Stage Snowblower",
            "true_cost": f"${price:.2f}",
            "advertised": f"MSRP ${MSRP:.0f}",
            "url": url,
            "fine_print": reason,
            "_deal_id": deal_id,
        })

    if deals_found:
        subject = f"DEAL ALERT: EGO SNT2416 Snowblower — ${deals_found[0]['true_cost'].lstrip('$')}"
        if len(deals_found) > 1:
            subject = f"DEAL ALERT: EGO SNT2416 — {len(deals_found)} listings found"
        rows = [{k: v for k, v in d.items() if not k.startswith("_")} for d in deals_found]
        send_deal_alert(subject, rows)
        for d in deals_found:
            mark_seen(MODULE, d["_deal_id"])
        print(f"[{MODULE}] Alerted {len(deals_found)} deal(s).")
    else:
        print(f"[{MODULE}] No deals found.")
        send_heartbeat(MODULE, "No deals found", f"Checked {len(unique_urls)} URLs. Threshold: ${PRICE_THRESHOLD}")

    record_heartbeat(MODULE)


if __name__ == "__main__":
    run()
