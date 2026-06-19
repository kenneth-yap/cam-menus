"""ETL pipeline orchestrator.

This is the conductor. It runs the components you already built — fetch, reduce,
extract, store — in sequence for every active college, and makes the decisions
between them. It is where the three hard requirements are enforced:

  1. Hash check   : only call the (paid, slow) LLM if the page actually changed.
  2. Date logic   : convert the LLM's "Monday" into a real calendar date.
  3. Status routing: every outcome (success / unchanged / failed) leaves an
                     honest record, and a failure NEVER corrupts good data.

Run it from the project root:
    python -m src.pipeline

Design note — the control loop defaults to safe on every fault. Each college is
wrapped so that any error records 'failed', leaves the previous good data in
place, and moves to the next college. One broken site cannot crash the run or
poison the database.
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()  # load .env before anything reads the environment

import hashlib
import logging
import os
import sys
from datetime import date, timedelta

# Your existing components. Adjust these imports to match your file layout.
from .storage import (
    get_connection,
    get_active_colleges,
    get_last_hash,
    save_successful_menu,
    save_run_status,
)
from .schema import CollegeMenu          # your Pydantic model
from .extract import fetch_html, html_to_text, extract_menu  # POC functions, ported

# ---------------------------------------------------------------------------
# Logging: every run writes a line. These logs ARE your research dataset later —
# status, timing, and which path each college took. Cheap to collect now.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log"),
    ],
)
log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Helper 1 — content fingerprint
# ---------------------------------------------------------------------------
def compute_hash(text: str) -> str:
    """A short, stable fingerprint of the reduced page text.

    sha256 gives the same hash for the same input every time. We compare today's
    hash to the last successful run's hash: identical means the page is unchanged,
    so we skip the LLM entirely. This is the 'only if updated' mechanism, applied
    where it saves money — the expensive extraction step.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helper 2 — day name to real date (deterministic; never the LLM's job)
# ---------------------------------------------------------------------------
_WEEKDAY_OFFSET = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def resolve_dates(menu: CollegeMenu) -> CollegeMenu:
    """Fill in each day's real calendar date from week_commencing + day name.

    The LLM returns a day NAME ('Monday') and ideally the week's start. Visitors
    think in real dates, so we convert here with plain arithmetic.

    Robustness: if the LLM did not return week_commencing (e.g. the page wrote
    '8th June' with no year, which the model declined to guess), we fall back to
    the CURRENT week's Monday. A weekly menu scraped this week belongs to this
    week, so that default is sound and keeps the pipeline working instead of
    discarding good menu data over a missing header.
    """
    wc = menu.week_commencing
    if isinstance(wc, str):
        try:
            wc = date.fromisoformat(wc)
        except ValueError:
            wc = None

    today = date.today()
    this_monday = today - timedelta(days=today.weekday())

    # Sanity-check the LLM's week_commencing. Pages like St John's write dates
    # with NO year ("Friday 19th Jun"), and the model then guesses a year — we
    # have seen it return 2020. A weekly menu scraped today cannot legitimately
    # sit years in the past or future. So we trust the LLM's date only if it's
    # within a sensible window of the current week; otherwise we discard it and
    # use this week's Monday. This treats the model's date as a claim to be
    # validated, not gospel.
    if wc is not None:
        days_off = abs((wc - this_monday).days)
        if days_off > 14:
            log.warning(
                "  LLM week_commencing %s is %d days from this week; "
                "distrusting it and using current week instead", wc, days_off)
            wc = None

    if wc is None:
        # Monday of the current week. date.weekday() is 0 for Monday.
        wc = this_monday
        log.warning("  defaulting week_commencing to this Monday %s", wc)

    for day in menu.days:
        key = (day.day or "").strip().lower()
        if key in _WEEKDAY_OFFSET:
            day.menu_date = wc + timedelta(days=_WEEKDAY_OFFSET[key])
        else:
            # No recognised weekday (e.g. Darwin's "Daily", or "Today"). These
            # are single-day "today's menu" pages, so we date them to today
            # rather than dropping them. Prevents a NOT NULL crash and keeps the
            # menu visible for the current day.
            day.menu_date = today
            log.warning("  day name %r not a weekday for %s; dating it today (%s)",
                        day.day, menu.college, today)
    return menu


# ---------------------------------------------------------------------------
# Helper 3 — sanity gate before we trust an extraction
# ---------------------------------------------------------------------------
def looks_valid(menu: CollegeMenu) -> bool:
    """A cheap plausibility check beyond Pydantic's structural validation.

    Pydantic confirms the SHAPE is right. This confirms the CONTENT is non-empty:
    at least one day, with at least one meal, with at least one dish. An empty but
    well-formed menu is treated as a failure, not saved as 'success' — otherwise
    the website would show a blank menu as if it were real.
    """
    if not menu.days:
        return False
    for day in menu.days:
        for sitting in day.meals:
            if sitting.dishes:
                return True
    return False


# ---------------------------------------------------------------------------
# The per-college unit of work
# ---------------------------------------------------------------------------
def process_college(conn, college: dict) -> str:
    """Run the full pipeline for one college. Returns the status string.

    Every failure path records a run and returns WITHOUT touching prior good
    data. The return value is for the summary log only.
    """
    name = college["name"]
    log.info("Processing %s", name)

    # JS-rendered sites need a headless browser we have not built yet. Record and
    # skip honestly rather than fetching an empty shell and 'succeeding' on junk.
    if college["fetch_mode"] == "js":
        save_run_status(conn, college["id"], "failed",
                        error_detail="js-rendered; headless fetch not implemented")
        log.info("  skipped (js-rendered)")
        return "skipped-js"

    # --- fetch + reduce -----------------------------------------------------
    try:
        raw = fetch_html(college["url"])
        text = html_to_text(raw)
    except Exception as exc:  # noqa: BLE001 — any fetch error is a safe-fail
        save_run_status(conn, college["id"], "failed", error_detail=f"fetch: {exc!r}")
        log.warning("  fetch failed: %s", exc)
        return "failed-fetch"

    # --- hash check: skip the LLM if nothing changed ------------------------
    new_hash = compute_hash(text)
    if new_hash == get_last_hash(conn, college["id"]):
        save_run_status(conn, college["id"], "unchanged", content_hash=new_hash)
        log.info("  unchanged since last run; LLM skipped")
        return "unchanged"

    # --- extract + validate -------------------------------------------------
    try:
        menu, model_used = extract_menu(name, text)   # (CollegeMenu, model string)
    except Exception as exc:  # noqa: BLE001
        save_run_status(conn, college["id"], "failed",
                        content_hash=new_hash, error_detail=f"extract: {exc!r}")
        log.warning("  extraction failed: %s", exc)
        return "failed-extract"

    menu = resolve_dates(menu)

    if not looks_valid(menu):
        save_run_status(conn, college["id"], "failed",
                        content_hash=new_hash, error_detail="empty/implausible menu")
        log.warning("  extraction returned nothing usable")
        return "failed-empty"

    # --- store (atomic) -----------------------------------------------------
    run_id = save_successful_menu(conn, college["id"], menu, new_hash, model_used)
    log.info("  saved successfully (run_id=%s, %d days, model=%s)",
             run_id, len(menu.days), model_used)
    return "success"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("=== pipeline run starting ===")
    conn = get_connection()
    summary: dict[str, int] = {}
    try:
        colleges = get_active_colleges(conn)
        log.info("%d active colleges", len(colleges))
        for college in colleges:
            # One college's crash must never stop the others. Belt-and-braces
            # around the already-defensive process_college.
            try:
                status = process_college(conn, college)
            except Exception as exc:  # noqa: BLE001
                log.exception("  UNHANDLED error for %s", college["name"])
                save_run_status(conn, college["id"], "failed",
                                error_detail=f"unhandled: {exc!r}")
                status = "failed-unhandled"
            summary[status] = summary.get(status, 0) + 1
    finally:
        conn.close()
    log.info("=== pipeline run finished: %s ===", summary)


if __name__ == "__main__":
    main()