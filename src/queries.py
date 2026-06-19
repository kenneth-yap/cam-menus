"""Read-side query layer for the website.

The website is purely a READER. It never scrapes or calls the LLM — it only
queries Postgres for what the ETL pipeline already stored. All the website's
business rules live here, in plain SQL plus a little Python:

  1. Next 5 days only      — filter menu_date to [today, today+4].
  2. Latest success only   — per college, read the most recent run with
                             status='success', ignoring failed/unchanged runs.
  3. Freshness gate        — if that success is older than FRESHNESS_DAYS, or the
                             college has no success at all, report 'unavailable'
                             rather than showing stale or missing data as if real.

Keeping these rules in one module means the fail-safe behaviour is enforced in a
single auditable place, exactly like the storage layer is the single writer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from .storage import get_connection

# A college whose newest successful run is older than this many days is treated
# as unavailable. Strict (2 days) per the project's fail-safe stance: better to
# say "unavailable" than to show a menu that may no longer be correct.
FRESHNESS_DAYS = 2

# How many days forward the website shows.
WINDOW_DAYS = 5


def _latest_success_per_college(conn: psycopg.Connection) -> dict[int, dict]:
    """For each active college, find its most recent SUCCESSFUL run.

    Returns {college_id: {"run_id", "run_at", "model_used", "name"}}.
    Colleges with no successful run simply won't appear in the dict — the caller
    treats their absence as 'unavailable'.

    DISTINCT ON is a Postgres feature: ordered by college then newest run, it
    keeps just the first (newest) row per college. It's the clean way to express
    "the latest row in each group".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (c.id)
                   c.id, c.name, r.id, r.run_at, r.model_used
            FROM colleges c
            JOIN menu_runs r ON r.college_id = c.id
            WHERE c.active = TRUE AND r.status = 'success'
            ORDER BY c.id, r.run_at DESC
            """
        )
        result = {}
        for college_id, name, run_id, run_at, model_used in cur.fetchall():
            result[college_id] = {
                "name": name,
                "run_id": run_id,
                "run_at": run_at,
                "model_used": model_used,
            }
        return result


def _menu_for_run(conn: psycopg.Connection, run_id: int,
                  start: date, end: date) -> list[dict]:
    """Pull the menu for one run, limited to the date window [start, end].

    Returns a list of day dicts, each with its meals and dishes nested inside,
    matching the shape the frontend renders. The ORDER BY keeps days, then
    meals, then dishes in a stable, readable order.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.menu_date, s.meal, di.name, di.course, di.dietary
            FROM day_menus d
            JOIN meal_sittings s ON s.day_menu_id = d.id
            JOIN dishes di       ON di.meal_sitting_id = s.id
            WHERE d.run_id = %s
              AND d.menu_date BETWEEN %s AND %s
            ORDER BY d.menu_date, s.meal, di.id
            """,
            (run_id, start, end),
        )
        rows = cur.fetchall()

    # Group the flat rows into nested days -> meals -> dishes.
    days: dict[date, dict] = {}
    for menu_date, meal, dish_name, course, dietary in rows:
        day = days.setdefault(
            menu_date, {"date": menu_date.isoformat(), "meals": {}}
        )
        sitting = day["meals"].setdefault(meal, [])
        sitting.append({
            "name": dish_name,
            "course": course,
            "dietary": dietary or [],
        })

    # Convert the meal dict into an ordered list for stable output.
    meal_order = ["breakfast", "brunch", "lunch", "dinner"]
    ordered_days = []
    for menu_date in sorted(days):
        day = days[menu_date]
        meals = [
            {"meal": m, "dishes": day["meals"][m]}
            for m in meal_order if m in day["meals"]
        ]
        ordered_days.append({"date": day["date"], "meals": meals})
    return ordered_days


def get_all_college_menus(conn: psycopg.Connection | None = None) -> dict:
    """The single function the API calls. Returns the full payload.

    Shape:
    {
      "generated_at": "2026-06-18",
      "window": {"start": "...", "end": "..."},
      "colleges": [
        {"name": "...", "status": "available", "updated": "...",
         "model_used": "...", "days": [...] },
        {"name": "...", "status": "unavailable",
         "reason": "no recent menu"},
        ...
      ]
    }
    The frontend just loops over "colleges" and renders each — available ones
    show their days, unavailable ones show a clear message.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        # Compute "today" in UK time, NOT the server's timezone. Render runs in
        # UTC, so date.today() there can be a day behind UK local time near
        # midnight, shifting the whole window. ZoneInfo pins us to UK time
        # (handling GMT/BST automatically) so the site always agrees with the
        # user's day.
        today = datetime.now(ZoneInfo("Europe/London")).date()
        end = today + timedelta(days=WINDOW_DAYS - 1)
        stale_before = today - timedelta(days=FRESHNESS_DAYS)

        latest = _latest_success_per_college(conn)

        # We want every ACTIVE college in the output, even those with no data,
        # so the site can explicitly show them as unavailable.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM colleges WHERE active = TRUE ORDER BY name"
            )
            active = cur.fetchall()

        colleges_out = []
        for college_id, name in active:
            info = latest.get(college_id)

            # No successful run at all -> unavailable.
            if info is None:
                colleges_out.append({
                    "name": name,
                    "status": "unavailable",
                    "reason": "no menu has been published yet",
                })
                continue

            # Has a success, but it's too old -> unavailable (fail-safe).
            if info["run_at"].date() < stale_before:
                colleges_out.append({
                    "name": name,
                    "status": "unavailable",
                    "reason": "menu may be out of date",
                })
                continue

            days = _menu_for_run(conn, info["run_id"], today, end)

            # Success exists and is fresh, but holds no days in our window
            # (e.g. weekend gap) -> still unavailable, honestly.
            if not days:
                colleges_out.append({
                    "name": name,
                    "status": "unavailable",
                    "reason": "no menu listed for the days ahead",
                })
                continue

            colleges_out.append({
                "name": name,
                "status": "available",
                "updated": info["run_at"].date().isoformat(),
                "model_used": info["model_used"],
                "days": days,
            })

        return {
            "generated_at": today.isoformat(),
            "window": {"start": today.isoformat(), "end": end.isoformat()},
            "colleges": colleges_out,
        }
    finally:
        if own_conn:
            conn.close()