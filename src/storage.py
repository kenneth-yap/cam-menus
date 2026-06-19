"""Storage layer: persist extracted menus into Postgres.

Design principle — the database is the buffer tank between the ETL pipeline and
the website. This module is the ONLY thing that writes to it. Keeping all writes
in one place means the fail-safe rule (a bad run never corrupts good data) is
enforced in exactly one auditable spot.

The load-bearing idea here is the TRANSACTION: a successful menu is written as an
all-or-nothing unit. If anything fails mid-write, the whole thing rolls back and
the database is untouched. A failed run still leaves an honest 'failed' record so
the website can say "unavailable" rather than showing stale data as if it were
fresh.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Optional

import psycopg

# NOTE: This module assumes the Pydantic models (CollegeMenu, DayMenu,
# MealSitting, Dish) are importable. In the real project they live in
# src/schema.py. For now, import them from wherever you keep them:
#   from .schema import CollegeMenu
# The functions below only rely on the attribute shape, so they stay decoupled
# from how the models are defined.


# ---------------------------------------------------------------------------
# 1. Connecting
# ---------------------------------------------------------------------------
def get_connection() -> psycopg.Connection:
    """Open a connection to Postgres.

    The connection string comes from the environment so no credentials live in
    code. Set DATABASE_URL in your .env, e.g.:
        DATABASE_URL=postgresql://menus:menus_dev_pw@localhost:5432/cam_menus

    psycopg.connect() returns a Connection object. We set autocommit=True so
    that each `with conn.transaction()` block commits permanently and survives
    the connection closing. The transaction blocks still provide all-or-nothing
    atomicity for each menu write.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL not set. In PowerShell:\n"
            '    $env:DATABASE_URL = '
            '"postgresql://menus:menus_dev_pw@localhost:5432/cam_menus"'
        )
    # autocommit=True is essential here. With it OFF, an outer transaction can
    # swallow the work done inside `with conn.transaction()` blocks, so closing
    # the connection discards rows that appeared to commit. With it ON, each
    # `with conn.transaction()` block is its own real, immediately-flushed
    # transaction that survives the connection closing. The transaction blocks
    # still give us all-or-nothing atomicity per menu.
    return psycopg.connect(dsn, autocommit=True)


# ---------------------------------------------------------------------------
# 2. Writing a SUCCESSFUL extraction (the atomic path)
# ---------------------------------------------------------------------------
def save_successful_menu(
    conn: psycopg.Connection,
    college_id: int,
    menu: "CollegeMenu",
    content_hash: str,
    model_used: str,
) -> int:
    """Write a full validated menu as ONE atomic transaction.

    Returns the new run_id.

    Every INSERT below happens inside a single `with conn.transaction()` block.
    If any line raises, psycopg rolls the whole block back automatically — no
    partial menu can ever reach the database. This is the interlock.
    """
    # `with conn.transaction():` opens a transaction. On clean exit it commits;
    # on any exception it rolls back. This one context manager is the entire
    # safety guarantee.
    with conn.transaction():
        # conn.cursor() gives us the object that actually executes SQL.
        with conn.cursor() as cur:
            # --- the run row -------------------------------------------------
            # %s are placeholders. NEVER use f-strings to build SQL — passing
            # values as the second argument lets psycopg escape them safely,
            # which is what prevents SQL injection. The values go in as a tuple.
            cur.execute(
                """
                INSERT INTO menu_runs (college_id, status, content_hash, model_used)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (college_id, "success", content_hash, model_used),
            )
            # RETURNING id makes the INSERT hand back the new primary key.
            # fetchone() returns a one-row tuple; [0] is the id itself.
            run_id = cur.fetchone()[0]

            # week_commencing lives on the MENU, not each day. Coerce the LLM's
            # string (e.g. '2026-06-08') to a real date once, here, for all days.
            wc = menu.week_commencing
            if isinstance(wc, str):
                from datetime import date as _date
                try:
                    wc = _date.fromisoformat(wc)
                except ValueError:
                    wc = None

            # --- each day, its sittings, and their dishes -------------------
            for day in menu.days:
                cur.execute(
                    """
                    INSERT INTO day_menus
                        (run_id, college_id, menu_date, week_commencing)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        run_id,
                        college_id,
                        day.menu_date,          # a real datetime.date, set in the pipeline
                        wc,                     # the menu-level week commencing
                    ),
                )
                day_id = cur.fetchone()[0]

                for sitting in day.meals:
                    cur.execute(
                        """
                        INSERT INTO meal_sittings (day_menu_id, meal)
                        VALUES (%s, %s)
                        RETURNING id
                        """,
                        (day_id, sitting.meal.value),  # .value -> the enum's string
                    )
                    sitting_id = cur.fetchone()[0]

                    # Dishes: instead of one INSERT per dish, batch them with
                    # executemany — fewer round-trips to the database, same
                    # transaction. A small but real efficiency win.
                    if sitting.dishes:
                        cur.executemany(
                            """
                            INSERT INTO dishes
                                (meal_sitting_id, name, course, dietary)
                            VALUES (%s, %s, %s, %s)
                            """,
                            [
                                (sitting_id, d.name, d.course, d.dietary)
                                for d in sitting.dishes
                            ],
                        )
        # Leaving the `with conn.transaction()` block here commits everything.
    return run_id


# ---------------------------------------------------------------------------
# 3. Writing a FAILED or UNCHANGED run (the honest-record path)
# ---------------------------------------------------------------------------
def save_run_status(
    conn: psycopg.Connection,
    college_id: int,
    status: str,
    content_hash: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> int:
    """Record a run that did NOT produce new menu data.

    Used for two cases:
      - status='failed'    : fetch/extract/validate blew up. error_detail says why.
      - status='unchanged' : content hash matched, so we skipped the LLM on purpose.

    Crucially this writes ONLY a menu_runs row — no day/dish rows. The previous
    successful run's data stays in place, untouched, as the known-good fallback.
    We never delete good data just because today's run failed.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO menu_runs
                    (college_id, status, content_hash, error_detail)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (college_id, status, content_hash, error_detail),
            )
            run_id = cur.fetchone()[0]
    return run_id


# ---------------------------------------------------------------------------
# 4. Small helpers the pipeline needs
# ---------------------------------------------------------------------------
def get_active_colleges(conn: psycopg.Connection) -> list[dict]:
    """Return the colleges to process. Adding a college = a row here, no code."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, url, fetch_mode FROM colleges WHERE active = TRUE"
        )
        # cur.fetchall() returns a list of tuples; we name the fields for clarity.
        return [
            {"id": r[0], "name": r[1], "url": r[2], "fetch_mode": r[3]}
            for r in cur.fetchall()
        ]


def get_last_hash(conn: psycopg.Connection, college_id: int) -> Optional[str]:
    """The content_hash of this college's most recent successful run, if any.

    This is what powers the 'only re-extract if the page changed' logic. The
    pipeline compares today's freshly-computed hash against this.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content_hash
            FROM menu_runs
            WHERE college_id = %s AND status = 'success'
            ORDER BY run_at DESC
            LIMIT 1
            """,
            (college_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None