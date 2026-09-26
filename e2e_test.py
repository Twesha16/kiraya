"""End-to-end test: runs the whole demo flow against the backend and checks the contract.

    .venv/bin/python e2e_test.py                      # local server (localhost:8000)
    .venv/bin/python e2e_test.py --url https://twiddling-empower-chain.ngrok-free.dev

It plays both voice agents: sends requirements (Seeker), fetches the listing and
posts a fake call result (Landlord), then waits for the simulator to finish.
NOTE: it resets the backend first, so don't run it during a live demo.
"""
import argparse
import json
import sys
import time

import httpx

HEADERS = {"ngrok-skip-browser-warning": "true"}

STATE_KEYS = {"stage", "requirements", "shortlist", "current_listing", "live_call_result",
              "simulated_results", "final_recommendation"}
SHORTLIST_KEYS = {"id", "pg_name", "area", "listed_rent", "food", "source_url"}
LISTING_KEYS = {"pg_name", "area", "listed_rent", "seeker_budget", "seeker_food", "seeker_move_in_date"}
SIM_KEYS = {"pg_name", "predicted_final_rent", "predicted_available", "predicted_is_match",
            "reasoning", "simulated"}
FINAL_KEYS = {"pg_name", "final_rent", "reason"}

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def has_keys(obj, keys, what):
    missing = keys - set(obj or {})
    check(not missing, f"{what} has all fields" + (f" (missing: {sorted(missing)})" if missing else ""))


def wait_for(client, stage, timeout):
    """Poll /state until it reaches `stage`, printing each stage change."""
    start, last = time.time(), None
    while time.time() - start < timeout:
        state = client.get("/state").json()
        if state["stage"] != last:
            last = state["stage"]
            print(f"  {time.time() - start:5.1f}s  stage = {last}")
        if last == stage:
            return state, time.time() - start
        time.sleep(1)
    return state, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--area", default="Ameerpet")
    parser.add_argument("--budget", type=int, default=9000)
    parser.add_argument("--food", default="veg", choices=["veg", "non-veg", "any"])
    args = parser.parse_args()

    client = httpx.Client(base_url=args.url.rstrip("/"), headers=HEADERS, timeout=30)
    print(f"Testing {args.url}\n")

    print("1. Health + reset")
    check(client.get("/health").json() == {"ok": True}, "GET /health returns {\"ok\": true}")
    client.post("/reset")
    state = client.get("/state").json()
    check(set(state) == STATE_KEYS, "GET /state has exactly the 7 contract keys")
    check(state["stage"] == "idle" and state["shortlist"] == [] and state["simulated_results"] == [],
          "state is idle with empty lists after reset")

    print("\n2. Seeker agent -> POST /requirements")
    req = {"area": args.area, "max_budget": args.budget, "food_preference": args.food,
           "move_in_date": "2026-10-15"}
    t = time.time()
    r = client.post("/requirements", json=req)
    check(r.status_code == 200 and r.json() == {"status": "ok"}, "returns {\"status\": \"ok\"}")
    check(time.time() - t < 2, f"responds immediately ({time.time() - t:.2f}s)")

    print("\n3. Search agent (waiting for shortlist_ready)")
    state, took = wait_for(client, "shortlist_ready", timeout=60)
    check(took is not None, "reached shortlist_ready within 60s")
    shortlist = state["shortlist"]
    check(3 <= len(shortlist) <= 5, f"shortlist has 3-5 listings (got {len(shortlist)})")
    for item in shortlist:
        has_keys(item, SHORTLIST_KEYS, f"listing #{item.get('id')} '{item.get('pg_name')}'")
        print(f"        {item.get('id')}. {item.get('pg_name')} | {item.get('area')} | "
              f"Rs {item.get('listed_rent')} | {item.get('food')}")
    if not shortlist:
        return finish(client)

    print("\n4. Landlord agent call start -> GET /current-listing")
    listing = client.get("/current-listing").json()
    print("        " + json.dumps(listing, ensure_ascii=False))
    has_keys(listing, LISTING_KEYS, "current listing")
    check(listing.get("pg_name") == shortlist[0]["pg_name"], "listing is shortlist #1")
    check(listing.get("seeker_budget") == args.budget and listing.get("seeker_move_in_date") == "2026-10-15",
          "seeker info merged in")
    check(client.get("/state").json()["stage"] == "calling", "stage is now 'calling'")

    print("\n5. Landlord agent call end -> POST /call-result")
    quoted = listing["listed_rent"]
    result = {"pg_name": listing["pg_name"], "rent_quoted": quoted, "final_rent": int(quoted * 0.92),
              "deposit": quoted * 2, "food_offered": "veg", "available": True,
              "is_match": int(quoted * 0.92) <= args.budget, "notes": "E2E test: agreed ~8% discount"}
    r = client.post("/call-result", json=result)
    check(r.status_code == 200 and r.json() == {"status": "ok"}, "returns {\"status\": \"ok\"}")

    print("\n6. Simulator (waiting for complete)")
    state, took = wait_for(client, "complete", timeout=60)
    check(took is not None, "reached complete within 60s")
    check(state["live_call_result"] and state["live_call_result"]["pg_name"] == listing["pg_name"],
          "live_call_result stored")
    sims = state["simulated_results"]
    check(len(sims) == len(shortlist) - 1, f"one simulated result per other listing ({len(sims)})")
    for s in sims:
        has_keys(s, SIM_KEYS, f"simulated '{s.get('pg_name')}'")
        check(s.get("simulated") is True, f"'{s.get('pg_name')}' marked simulated: true")
    final = state["final_recommendation"]
    has_keys(final, FINAL_KEYS, "final_recommendation")

    return finish(client)


def finish(client):
    print("\n=== Final /state ===")
    print(json.dumps(client.get("/state").json(), indent=2, ensure_ascii=False))
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL CHECKS PASSED")
    print("(The backend is left in 'complete' state. Reset before the demo: POST /reset)")


if __name__ == "__main__":
    main()
