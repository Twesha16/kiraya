"""Search agent: requirements -> web search (Tavily) -> Sarvam-105B extraction -> filter & rank.

Run this file directly to rebuild fallback_shortlist.json:
    .venv/bin/python search_agent.py
"""
import asyncio
import json
import os
import re

import httpx

from sarvam import chat_json  # also loads .env

TAVILY_URL = "https://api.tavily.com/search"
FALLBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fallback_shortlist.json")
SEARCH_TIMEOUT = 25  # seconds; after this we use the fallback file

# Used only if fallback_shortlist.json is missing too
LAST_RESORT = [
    {"id": 1, "pg_name": "Sri Balaji PG", "area": "Ameerpet", "listed_rent": 10000,
     "food": "veg", "source_url": "https://example.com/sri-balaji-pg"},
    {"id": 2, "pg_name": "Sai Krupa Men's Hostel", "area": "Ameerpet", "listed_rent": 8500,
     "food": "veg", "source_url": "https://example.com/sai-krupa"},
    {"id": 3, "pg_name": "Green Park Residency PG", "area": "SR Nagar", "listed_rent": 9000,
     "food": "both", "source_url": "https://example.com/green-park"},
]


# ---------- a) queries ----------

def build_queries(req):
    area = req["area"]
    food = {"veg": "veg food", "non-veg": "non-veg food", "any": "with food"}[req["food_preference"]]
    return [
        f"PG in {area} Hyderabad {food} rent per month",
        f"paying guest hostel {area} Hyderabad under Rs {req['max_budget']}",
        f"best PG hostels near {area} Hyderabad contact rent",
    ]


# ---------- b) Tavily ----------

async def tavily_search(client, query):
    r = await client.post(
        TAVILY_URL,
        headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"]},
        json={"query": query, "search_depth": "basic", "max_results": 6, "include_raw_content": True},
    )
    r.raise_for_status()
    return r.json().get("results", [])


async def web_search(queries):
    """Run all queries in parallel; return unique results (by URL)."""
    async with httpx.AsyncClient(timeout=15) as client:
        batches = await asyncio.gather(*(tavily_search(client, q) for q in queries), return_exceptions=True)
    seen, results = set(), []
    for batch in batches:
        if isinstance(batch, Exception):
            print(f"[search] a Tavily query failed: {type(batch).__name__}: {batch}")
            continue
        for item in batch:
            if item.get("url") and item["url"] not in seen:
                seen.add(item["url"])
                results.append(item)
    return results


# ---------- c) Sarvam extraction ----------

EXTRACT_SYSTEM = """You extract paying-guest (PG) / hostel listings in Hyderabad from web search results.
Rules:
- Only include specific, named PGs or hostels whose name appears in the text. Never invent names or prices.
- pg_name must be a proper business name (e.g. "Sai Dinesh Boys PG"), NOT a generic description
  (e.g. NOT "PG for Boys in Ameerpet", NOT "Single room PG").
- listed_rent: monthly rent in rupees as an integer (the lowest rent stated for that PG). Skip a PG if no rent is stated.
- food: "veg", "non-veg", "both", or "unknown" if not stated.
- area: the Hyderabad locality of the PG.
- source_url: the URL of the search result the PG came from (copy it exactly).
- Return at most 8 listings: prefer ones in or near the seeker's area, with rent close to their budget.
Return: {"listings": [{"pg_name": str, "area": str, "listed_rent": int, "food": str, "source_url": str}]}"""


GENERIC_NAME = re.compile(
    r"^(single|double|triple|shared|private|luxury|budget|boys|girls|ladies|gents|mens|women'?s|co-?living)?\s*"
    r"(room\s*)?(pg|hostel|paying guest|accommodation)s?(\s*/\s*paying guest)?"
    r"(\s+(for|in|near|at)\b.*)?$",
    re.IGNORECASE)


PRICE_LINE = re.compile(r"(₹|rs\.?\s*\d|inr|/\s*month|per month|rent|pg|hostel|veg|food)", re.IGNORECASE)


def useful_text(result, limit=2000):
    """Tavily's snippet + only the page lines that mention prices/PGs.
    (The top of a page is mostly menus, the listings are further down.)"""
    snippet = result.get("content") or ""
    lines = [l.strip() for l in (result.get("raw_content") or "").splitlines()]
    lines = [l for l in lines if 8 < len(l) < 300 and PRICE_LINE.search(l)]
    return (snippet + "\n" + "\n".join(dict.fromkeys(lines)))[:limit]


async def extract_listings(results, req):
    chunks = []
    for i, r in enumerate(results[:12]):
        text = useful_text(r)
        chunks.append(f"### Result {i + 1}\nURL: {r['url']}\nTitle: {r.get('title', '')}\n{text}")
    user = (f"Seeker is looking near {req['area']}, Hyderabad, budget Rs {req['max_budget']}/month, "
            f"food: {req['food_preference']}.\n\n" + "\n\n".join(chunks))
    data = await chat_json(EXTRACT_SYSTEM, user, max_tokens=3000, timeout=SEARCH_TIMEOUT)
    if not data or not isinstance(data.get("listings"), list):
        return []

    valid_urls = {r["url"] for r in results}
    clean = []
    for x in data["listings"]:
        try:
            rent = int(float(str(x.get("listed_rent")).replace(",", "").replace("₹", "")))
        except (ValueError, TypeError):
            continue
        name = str(x.get("pg_name") or "").strip()
        url = str(x.get("source_url") or "")
        if not name or rent < 1000 or rent > 100000 or url not in valid_urls:
            continue  # drop junk / made-up entries
        if GENERIC_NAME.match(name):
            continue  # "PG for Boys in Ameerpet" is not a real listing
        food = str(x.get("food") or "unknown").lower()
        if food not in ("veg", "non-veg", "both"):
            food = "unknown"
        clean.append({"pg_name": name, "area": str(x.get("area") or req["area"]).strip(),
                      "listed_rent": rent, "food": food, "source_url": url})
    return clean


# ---------- d) filter & rank ----------

GENERIC_WORDS = {"pg", "hostel", "hostels", "paying", "guest", "boys", "girls", "mens", "men", "womens",
                 "women", "ladies", "gents", "executive", "deluxe", "luxury", "the", "and", "for", "co", "living"}


def name_key(name):
    words = re.findall(r"[a-z0-9]+", name.lower().replace("'s", ""))
    return " ".join(w for w in words if w not in GENERIC_WORDS) or name.lower()


def filter_and_rank(listings, req, keep=5):
    budget, pref, area = req["max_budget"], req["food_preference"], req["area"].lower()

    def food_ok(food):
        if pref == "any" or food in ("both", "unknown"):
            return True
        return food == pref

    # Allow up to 25% over budget: the landlord call can negotiate down
    kept = [x for x in listings if x["listed_rent"] <= budget * 1.25 and food_ok(x["food"])]

    # De-duplicate by name, ignoring generic words ("Vaibhav PG" == "Vaibhav Boys Hostel (PG)")
    unique = {}
    for x in kept:
        unique.setdefault(name_key(x["pg_name"]), x)

    def score(x):
        return (
            area not in x["area"].lower(),   # right area first
            x["food"] == "unknown",           # known food before unknown
            abs(x["listed_rent"] - budget),   # closest to budget first
        )

    ranked = sorted(unique.values(), key=score)[:keep]
    return [{"id": i + 1, **x} for i, x in enumerate(ranked)]


# ---------- the full agent ----------

async def live_search(req):
    queries = build_queries(req)
    results = await web_search(queries)
    print(f"[search] {len(results)} web results for {queries}")
    if not results:
        return []
    listings = await extract_listings(results, req)
    print(f"[search] Sarvam extracted {len(listings)} listings")
    return filter_and_rank(listings, req)


def load_fallback():
    try:
        with open(FALLBACK_FILE) as f:
            data = json.load(f)
        if data:
            return data
    except Exception:
        pass
    return [dict(x) for x in LAST_RESORT]


async def find_shortlist(req):
    """Live search with a timeout; silently falls back to the saved shortlist."""
    try:
        shortlist = await asyncio.wait_for(live_search(req), timeout=SEARCH_TIMEOUT)
        if len(shortlist) >= 3:
            print(f"[search] using LIVE shortlist ({len(shortlist)} listings)")
            return shortlist
        if shortlist:
            # Too few for a good demo: top up with saved listings (no duplicates)
            have = {name_key(x["pg_name"]) for x in shortlist}
            extra = [x for x in load_fallback() if name_key(x["pg_name"]) not in have]
            combined = (shortlist + extra)[:5]
            print(f"[search] only {len(shortlist)} live listings -> topped up from fallback")
            return [{**x, "id": i + 1} for i, x in enumerate(combined)]
        print("[search] live search found nothing -> using fallback")
    except asyncio.TimeoutError:
        print(f"[search] live search took over {SEARCH_TIMEOUT}s -> using fallback")
    except Exception as e:
        print(f"[search] live search failed ({type(e).__name__}: {e}) -> using fallback")
    return load_fallback()


async def build_fallback(req, runs=3):
    """Web results vary run to run, so merge a few runs for a solid fallback."""
    merged = []
    for i in range(runs):
        results = await web_search(build_queries(req))
        found = await extract_listings(results, req)
        print(f"[fallback] run {i + 1}: {len(found)} listings extracted")
        merged += found
    return filter_and_rank(merged, req)


if __name__ == "__main__":
    # Build fallback_shortlist.json from real searches (no timeout here)
    req = {"area": "Ameerpet", "max_budget": 9000, "food_preference": "veg", "move_in_date": "2026-10-01"}
    shortlist = asyncio.run(build_fallback(req))
    print(json.dumps(shortlist, indent=2, ensure_ascii=False))
    if shortlist:
        with open(FALLBACK_FILE, "w") as f:
            json.dump(shortlist, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(shortlist)} listings to fallback_shortlist.json")
    else:
        print("Nothing found - fallback file NOT written")
