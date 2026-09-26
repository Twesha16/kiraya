"""Simulator: after the one real landlord call, predict how calls to the other
shortlisted PGs would go (Sarvam-105B), then pick a final recommendation.
"""
import asyncio

from sarvam import chat_json
from search_agent import name_key

SIM_TIMEOUT = 20  # seconds for all predictions together

SIM_SYSTEM = """You simulate phone negotiations between a PG (paying guest) seeker in Hyderabad and a PG landlord.
You are given: one shortlisted PG listing, the seeker's requirements, and the result of a REAL call
the seeker's agent already made to a different PG in the same search (use it to calibrate how much
landlords in this market negotiate).
Predict the outcome of a similar call to THIS listing.
- predicted_final_rent: integer rupees/month after negotiation (usually a bit below listed rent, never far below).
- predicted_available: whether a bed is free on the seeker's move-in date. This is NOT about price.
  Most Hyderabad PGs have vacancies, so predict true unless there is a clear reason not to.
- predicted_is_match: true only if the final rent is within the seeker's budget AND food fits their preference.
  If the listing's food is "unknown", the listing just didn't mention it: predict what the landlord would
  say on the phone (most Hyderabad PGs serve veg or both), don't treat unknown as a mismatch.
- reasoning: ONE short sentence explaining the prediction, consistent with the numbers you give.
Return: {"predicted_final_rent": int, "predicted_available": bool, "predicted_is_match": bool, "reasoning": str}"""


def food_fits(food, pref):
    return pref == "any" or food in ("both", "unknown") or food == pref


def rule_based(listing, req, live):
    """Fallback if the LLM fails: apply the same discount the real landlord gave."""
    discount = 1.0
    if live and live.get("rent_quoted"):
        discount = max(0.8, min(1.0, live["final_rent"] / live["rent_quoted"]))
    rent = int(round(listing["listed_rent"] * discount / 100) * 100)
    is_match = rent <= req["max_budget"] and food_fits(listing["food"], req["food_preference"])
    pct = round((1 - discount) * 100)
    return {
        "pg_name": listing["pg_name"],
        "predicted_final_rent": rent,
        "predicted_available": True,
        "predicted_is_match": is_match,
        "reasoning": f"Estimate: applied the {pct}% discount seen in the live call to the listed Rs {listing['listed_rent']}.",
        "simulated": True,
    }


async def predict(listing, req, live):
    fallback = rule_based(listing, req, live)
    user = (
        f"Listing: {listing}\n"
        f"Seeker requirements: {req}\n"
        f"Real call result (different PG): {live}"
    )
    data = await chat_json(SIM_SYSTEM, user, max_tokens=400, timeout=SIM_TIMEOUT)
    if not data:
        return fallback
    try:
        rent = int(float(data["predicted_final_rent"]))
        available = bool(data["predicted_available"])
        reasoning = str(data.get("reasoning") or "").strip() or fallback["reasoning"]
    except (KeyError, ValueError, TypeError):
        return fallback
    # Sanity check: keep predictions believable
    if not (0.6 * listing["listed_rent"] <= rent <= 1.1 * listing["listed_rent"]):
        return fallback
    is_match = (bool(data.get("predicted_is_match")) and available
                and rent <= req["max_budget"] and food_fits(listing["food"], req["food_preference"]))
    return {
        "pg_name": listing["pg_name"],
        "predicted_final_rent": rent,
        "predicted_available": available,
        "predicted_is_match": is_match,
        "reasoning": reasoning,
        "simulated": True,
    }


async def simulate_all(shortlist, req, live, called_name):
    """Predict outcomes for every shortlisted PG except the one really called."""
    skip = {name_key(called_name or ""), name_key(live.get("pg_name") or "")}
    others = [x for x in shortlist if name_key(x["pg_name"]) not in skip]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(predict(x, req, live) for x in others)), timeout=SIM_TIMEOUT + 5)
        print(f"[simulator] predicted {len(results)} listings")
        return list(results)
    except Exception as e:
        print(f"[simulator] failed ({type(e).__name__}) -> rule-based predictions")
        return [rule_based(x, req, live) for x in others]


def pick_final(live, simulated):
    """Prefer is_match == True, then a confirmed (live) price over a predicted one,
    then lowest final rent. Available listings only."""
    candidates = []
    if live and live.get("available"):
        candidates.append((live["is_match"], True, live["final_rent"], live["pg_name"], "confirmed on the live call"))
    for s in simulated:
        if s["predicted_available"]:
            candidates.append((s["predicted_is_match"], False, s["predicted_final_rent"], s["pg_name"], "simulated prediction"))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (not c[0], not c[1], c[2]))
    is_match, _, rent, name, source = candidates[0]
    verdict = "Best match for budget and food" if is_match else "Closest option, though not a full match"
    return {"pg_name": name, "final_rent": rent,
            "reason": f"{verdict} at Rs {rent}/month ({source})."}
