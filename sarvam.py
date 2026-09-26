"""Tiny helper for calling Sarvam-105B and getting JSON back.

Docs: https://docs.sarvam.ai/api-reference/chat/chat-completions-v1
"""
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SARVAM_URL = "https://api.sarvam.ai/v1/chat/completions"
SARVAM_MODEL = "sarvam-105b"


def parse_json(text):
    """Parse JSON from model output, even if wrapped in ``` fences or extra text.
    Returns None if nothing parseable is found."""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Fall back to the outermost {...} block
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    return None


async def chat_json(system_prompt, user_prompt, max_tokens=4096, timeout=30):
    """Ask Sarvam-105B for a JSON object. Returns a dict, or None on any failure."""
    key = os.environ.get("SARVAM_API_KEY")
    if not key:
        print("[sarvam] SARVAM_API_KEY missing from .env")
        return None
    body = {
        "model": SARVAM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt + "\nRespond with a single valid JSON object only. No markdown, no explanation."},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        # Reasoning OFF: with it on, the model can burn all max_tokens thinking
        # (30s+, empty content). Off -> a few seconds and clean JSON.
        "reasoning_effort": None,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(SARVAM_URL, headers={"api-subscription-key": key}, json=body)
        if r.status_code != 200:
            print(f"[sarvam] HTTP {r.status_code}: {r.text[:300]}")
            return None
        content = r.json()["choices"][0]["message"].get("content")
        data = parse_json(content)
        if not isinstance(data, dict):
            print(f"[sarvam] could not parse JSON from: {str(content)[:300]}")
            return None
        return data
    except Exception as e:
        print(f"[sarvam] request failed: {type(e).__name__}: {e}")
        return None
