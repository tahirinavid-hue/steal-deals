"""
Scraping helpers.

Strategy:
  - Use Playwright (local headless Chromium) for standard pages — free.
  - Fall back to ScrapingBee for Cloudflare / heavy anti-bot pages — costs credits.

Usage:
  from shared.proxy import fetch_page
  html = fetch_page("https://example.com", use_js=True, force_api=False)
"""
import os
import requests
from playwright.sync_api import sync_playwright

SCRAPINGBEE_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "")
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"

# Default headers to look like a real browser
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_page(url: str, use_js: bool = False, force_api: bool = False) -> str:
    """
    Fetch a URL and return the full HTML as a string.

    Args:
        url:       Target URL.
        use_js:    If True and not force_api, use Playwright to render JS.
        force_api: Always route through ScrapingBee (use for Cloudflare-protected pages).
    """
    if force_api:
        return _fetch_scrapingbee(url)
    if use_js:
        return _fetch_playwright(url)
    return _fetch_requests(url)


def _fetch_requests(url: str) -> str:
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_playwright(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers=DEFAULT_HEADERS)
        page.goto(url, wait_until="networkidle", timeout=60_000)
        html = page.content()
        browser.close()
    return html


def _fetch_scrapingbee(url: str) -> str:
    if not SCRAPINGBEE_KEY:
        raise EnvironmentError("SCRAPINGBEE_API_KEY is not set")
    resp = requests.get(
        SCRAPINGBEE_URL,
        params={
            "api_key": SCRAPINGBEE_KEY,
            "url": url,
            "render_js": "true",
            "premium_proxy": "true",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


def google_search_urls(query: str, num: int = 8) -> list[str]:
    """
    Return a list of result URLs from a Google search using SerpAPI-style
    scraping via ScrapingBee's Google endpoint.

    Falls back to a simple DuckDuckGo HTML scrape if no API key is available.
    """
    if SCRAPINGBEE_KEY:
        resp = requests.get(
            SCRAPINGBEE_URL,
            params={
                "api_key": SCRAPINGBEE_KEY,
                "url": f"https://www.google.com/search?q={requests.utils.quote(query)}&num={num}",
                "render_js": "false",
                "premium_proxy": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        html = resp.text
    else:
        html = _fetch_requests(
            f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
        )

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for a in soup.select("a[href]"):
        href = a["href"]
        # Google wraps URLs in /url?q=... ; DuckDuckGo uses direct hrefs
        if "/url?q=" in href:
            href = href.split("/url?q=")[1].split("&")[0]
        if href.startswith("http") and "google.com" not in href:
            urls.append(href)
        if len(urls) >= num:
            break
    return urls
