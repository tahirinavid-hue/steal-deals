"""
Upstash Redis client for seen_deals state management.
Uses the HTTP REST API — no persistent TCP connection needed in serverless environments.
"""
import os
import time
import hashlib
import requests

UPSTASH_URL = os.environ["UPSTASH_REDIS_REST_URL"]
UPSTASH_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]

# How long (seconds) before the same deal can re-alert (7 days)
DEDUP_TTL = 60 * 60 * 24 * 7

HEADERS = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}


def _cmd(*args):
    """Execute a raw Redis command via Upstash REST API."""
    url = f"{UPSTASH_URL}/{'/'.join(str(a) for a in args)}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result")


def make_deal_key(module: str, identifier: str) -> str:
    """Create a short, stable Redis key for a deal."""
    digest = hashlib.md5(identifier.encode()).hexdigest()[:12]
    return f"seen:{module}:{digest}"


def is_seen(module: str, identifier: str) -> bool:
    """Return True if this deal was already alerted within the dedup window."""
    key = make_deal_key(module, identifier)
    return _cmd("EXISTS", key) == 1


def mark_seen(module: str, identifier: str) -> None:
    """Record a deal as seen; expires after DEDUP_TTL seconds."""
    key = make_deal_key(module, identifier)
    _cmd("SET", key, int(time.time()), "EX", DEDUP_TTL)


def record_heartbeat(module: str) -> None:
    """Update the last-run timestamp for a module (used by heartbeat emails)."""
    _cmd("SET", f"heartbeat:{module}", int(time.time()))


def get_heartbeat(module: str):
    """Return last-run epoch for a module, or None."""
    val = _cmd("GET", f"heartbeat:{module}")
    return int(val) if val else None
