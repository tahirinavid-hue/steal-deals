"""
LLM extraction helper using Google Gemini Flash 2.0.
Only called from Tier 2 modules (B and C) — never from Module A.
"""
import os
import json
import re
import time
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.0-flash"
MAX_RETRIES = 4
BASE_DELAY = 15  # seconds between retries


def extract_structured(prompt: str, page_text: str, schema_hint: str = "") -> dict:
    """
    Send page_text + prompt to Gemini and return a parsed JSON dict.
    Retries up to MAX_RETRIES times on rate-limit (429) errors.
    """
    full_prompt = f"""{prompt}

{f'Output format (JSON): {schema_hint}' if schema_hint else 'Return a JSON object only, no markdown fences.'}

--- PAGE TEXT START ---
{page_text[:12000]}
--- PAGE TEXT END ---
"""
    model = genai.GenerativeModel(MODEL)

    for attempt in range(MAX_RETRIES):
        try:
            response = model.generate_content(full_prompt)
            raw = response.text.strip()
            break
        except ResourceExhausted as e:
            if attempt < MAX_RETRIES - 1:
                wait = BASE_DELAY * (attempt + 1)
                print(f"[llm] Rate limited, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                raise
    raw = response.text.strip()

    # Strip markdown fences if model includes them
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Best-effort: return raw text under a key so callers can inspect
        return {"_raw": raw, "_parse_error": True}


def strip_html(html: str) -> str:
    """Naive but fast HTML tag stripper — avoids importing heavy libs."""
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
