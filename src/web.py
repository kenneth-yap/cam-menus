"""FastAPI web service — the read-only API for the menu website.

This is the contract a frontend (or any other consumer) talks to. It does no
scraping and no LLM calls; it only reads from Postgres via the queries module.

Endpoints:
  GET /api/menus   -> the full JSON payload (all colleges, next 5 days)
  GET /api/health  -> a simple liveness check
  GET /            -> serves the static frontend page

Run it from the project root with:
    uvicorn src.web:app --reload

Then open http://127.0.0.1:8000 in a browser.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # load DATABASE_URL before anything reads the environment

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .queries import get_all_college_menus

app = FastAPI(title="Cambridge College Menus", version="1.0")

# The static page lives next to this file in src/static/index.html.
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/api/menus")
def api_menus() -> JSONResponse:
    """Return all active colleges with their next-5-days menus or status.

    The heavy lifting (freshness rules, latest-success-only, date window) is in
    the queries module. This endpoint is a thin wrapper so the business logic
    stays in one testable place.
    """
    payload = get_all_college_menus()
    return JSONResponse(payload)


@app.get("/api/health")
def api_health() -> dict:
    """Liveness check. Returns ok if the service is up and can reach the DB."""
    try:
        # A tiny query proves the database connection works, not just the app.
        payload = get_all_college_menus()
        return {"status": "ok", "colleges": len(payload["colleges"])}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": repr(exc)}


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page frontend."""
    return FileResponse(_STATIC_DIR / "index.html")
