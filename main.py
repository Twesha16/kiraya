"""PG Finder backend. Search agent lives in search_agent.py, Sarvam helper in sarvam.py.

Run:  uvicorn main:app --reload --port 8000
"""
import asyncio
import os
import re

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AliasChoices, BaseModel, Field, field_validator
from typing import Literal, Optional

from search_agent import find_shortlist
from simulator import simulate_all, pick_final

app = FastAPI(title="PG Finder Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def log_bad_request(request: Request, exc: RequestValidationError):
    """Print rejected requests so integration problems are easy to spot."""
    body = (await request.body()).decode(errors="replace")[:500]
    print(f"[422] {request.method} {request.url.path} body={body}")
    for e in exc.errors():
        print(f"      field {'.'.join(map(str, e['loc'][1:]))}: {e['msg']}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ---------- Request bodies (field names match the contract exactly) ----------

# Voice agents often send "9,000" / "Veg" / "vegetarian" / "True" - clean those up
# before validation instead of rejecting the request mid-demo.

def to_int(v):
    if isinstance(v, str):
        digits = re.sub(r"[^\d.]", "", v)
        return int(float(digits)) if digits else 0
    return v


FOOD_WORDS = {"vegetarian": "veg", "nonveg": "non-veg", "non veg": "non-veg", "non-vegetarian": "non-veg",
              "nonvegetarian": "non-veg", "both": "both", "no": "none", "no food": "none", "any": "any"}


def to_food(v):
    v = str(v or "").strip().lower()
    return FOOD_WORDS.get(v, v)


def to_food_offered(v):
    return to_food(v) or "none"


def to_bool(v):
    if isinstance(v, str) and v.strip() == "":
        return False  # variable never got filled
    return v


class Requirements(BaseModel):
    area: str
    max_budget: int
    # The Seeker agent's tool sends "food_preferences" (plural) - accept both
    food_preference: Literal["veg", "non-veg", "any"] = Field(
        validation_alias=AliasChoices("food_preference", "food_preferences"))
    move_in_date: str

    _ints = field_validator("max_budget", mode="before")(to_int)
    _food = field_validator("food_preference", mode="before")(to_food)


class CallResult(BaseModel):
    pg_name: str
    rent_quoted: int
    final_rent: int
    deposit: int = 0
    food_offered: Literal["veg", "non-veg", "both", "none"]
    available: bool
    is_match: bool
    notes: Optional[str] = ""

    _ints = field_validator("rent_quoted", "final_rent", "deposit", mode="before")(to_int)
    _food = field_validator("food_offered", mode="before")(to_food_offered)
    _bools = field_validator("available", "is_match", mode="before")(to_bool)


# ---------- In-memory state ----------

def empty_state():
    return {
        "stage": "idle",
        "requirements": None,
        "shortlist": [],
        "current_listing": None,
        "live_call_result": None,
        "simulated_results": [],
        "final_recommendation": None,
    }


STATE = empty_state()
RUN_ID = 0  # bumps on /reset so old background jobs stop writing to state


# ---------- Search agent (Phase 3) ----------

async def run_search(run_id: int):
    """Real search agent (Tavily + Sarvam-105B), falls back to saved listings."""
    await asyncio.sleep(1.5)  # let the frontend show "requirements_received"
    if run_id != RUN_ID:
        return
    STATE["stage"] = "searching"

    shortlist = await find_shortlist(STATE["requirements"])
    if run_id != RUN_ID:
        return
    STATE["shortlist"] = shortlist
    STATE["stage"] = "shortlist_ready"


async def run_simulator(run_id: int):
    """Real simulator (Sarvam-105B), falls back to rule-based predictions."""
    await asyncio.sleep(1.5)  # let the frontend show "call_done"
    if run_id != RUN_ID:
        return
    STATE["stage"] = "simulating"

    live = STATE["live_call_result"]
    req = STATE["requirements"]
    simulated = []
    if req and STATE["shortlist"]:
        called = (STATE["current_listing"] or {}).get("pg_name")
        simulated = await simulate_all(STATE["shortlist"], req, live, called)
    if run_id != RUN_ID:
        return
    STATE["simulated_results"] = simulated
    STATE["final_recommendation"] = pick_final(live, simulated)
    STATE["stage"] = "complete"


# ---------- Endpoints ----------

FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend.html")


@app.get("/")
def frontend():
    """The demo page. Open the ngrok URL in a browser to see it."""
    return FileResponse(FRONTEND)


# ---------- Voice calls from the web page ----------
# The page runs Sarvam's browser SDK, but the Voice Agents API key must never reach the
# browser. The SDK's only keyed request is "get a signed WebSocket URL"; we point the SDK
# at /voice/ and make that one request here with the real key. The browser only ever sees
# the short-lived signed URL, then streams audio straight to Sarvam.

# IDs (not secrets) of the Voice Agents workspace; override in .env to use your own agents
VOICE_ORG = os.environ.get("SARVAM_ORG_ID", "01a0dcda-f908-7199-8e8a-43b9a165a32c")
VOICE_WORKSPACE = os.environ.get("SARVAM_WORKSPACE_ID", "01a0dcda-f90e-79c3-aa57-f65fa772a85b")
VOICE_APPS = {"seeker": os.environ.get("SEEKER_APP_ID", "Seeker-Agen-971d3ffe-1d08"),
              "landlord": os.environ.get("LANDLORD_APP_ID", "Landlord-Ag-8526a55c-8b6b")}


def voice_key():
    env = dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))  # re-read: no restart needed
    return env.get("SARVAM_VOICE_KEY") or ""


@app.get("/voice-config")
def voice_config():
    """IDs the page needs to start a call. No secrets in here."""
    return {"enabled": bool(voice_key()), "org_id": VOICE_ORG, "workspace_id": VOICE_WORKSPACE,
            "seeker_app_id": VOICE_APPS["seeker"], "landlord_app_id": VOICE_APPS["landlord"]}


@app.get("/voice/orgs/{org}/workspaces/{ws}/apps/{app_id}/url")
async def voice_signed_url(org: str, ws: str, app_id: str, request: Request):
    """Fetch a signed call URL from Sarvam using the server-side key (only for our two agents)."""
    if org != VOICE_ORG or ws != VOICE_WORKSPACE or app_id not in VOICE_APPS.values():
        return JSONResponse(status_code=403, content={"error": "not one of our agents"})
    if not voice_key():
        return JSONResponse(status_code=503, content={"error": "SARVAM_VOICE_KEY missing in .env"})
    url = f"https://apps.sarvam.ai/api/app-runtime/orgs/{org}/workspaces/{ws}/apps/{app_id}/url"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=dict(request.query_params), headers={"X-API-Key": voice_key()})
    if r.status_code != 200:
        print(f"[voice] Sarvam returned {r.status_code}: {r.text[:200]}")
    return JSONResponse(status_code=r.status_code, content=r.json() if "json" in r.headers.get("content-type", "") else {"error": r.text[:300]})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/state")
def get_state():
    return STATE


@app.post("/requirements")
async def post_requirements(req: Requirements, background: BackgroundTasks):
    global RUN_ID
    RUN_ID += 1
    STATE.update(empty_state())
    STATE["requirements"] = req.model_dump()
    STATE["stage"] = "requirements_received"
    background.add_task(run_search, RUN_ID)
    return {"status": "ok"}


@app.get("/current-listing")
def current_listing():
    if not STATE["shortlist"]:
        return {"error": "no shortlist yet"}
    top = STATE["shortlist"][0]
    req = STATE["requirements"] or {}
    listing = {
        "pg_name": top["pg_name"],
        "area": top["area"],
        "listed_rent": top["listed_rent"],
        "seeker_budget": req.get("max_budget"),
        "seeker_food": req.get("food_preference"),
        "seeker_move_in_date": req.get("move_in_date"),
    }
    STATE["current_listing"] = listing
    STATE["stage"] = "calling"
    return listing


@app.post("/call-result")
async def post_call_result(result: CallResult, background: BackgroundTasks):
    live = result.model_dump()
    # Fill gaps if some voice-agent variables arrived empty
    called = STATE["current_listing"] or {}
    live["pg_name"] = live["pg_name"] or called.get("pg_name", "")
    live["rent_quoted"] = live["rent_quoted"] or called.get("listed_rent") or live["final_rent"]
    live["final_rent"] = live["final_rent"] or live["rent_quoted"]
    STATE["live_call_result"] = live
    STATE["stage"] = "call_done"
    background.add_task(run_simulator, RUN_ID)
    return {"status": "ok"}


@app.post("/reset")
def reset():
    global RUN_ID
    RUN_ID += 1
    STATE.update(empty_state())
    return {"status": "ok"}
