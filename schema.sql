-- schema.sql

-- Colleges are configuration. Adding a college = inserting a row here.
CREATE TABLE colleges (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    fetch_mode  TEXT NOT NULL DEFAULT 'http',   -- 'http' or 'js'
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

-- One row per extraction attempt per college. This is your audit log and
-- the heart of the "is it fresh / did it fail" logic.
CREATE TABLE menu_runs (
    id            SERIAL PRIMARY KEY,
    college_id    INTEGER NOT NULL REFERENCES colleges(id),
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL,                -- 'success' | 'failed' | 'unchanged'
    content_hash  TEXT,                         -- fingerprint of the reduced page
    error_detail  TEXT                          -- why it failed, if it did
);

-- A concrete dated day of menu, tied to the run that produced it.
CREATE TABLE day_menus (
    id               SERIAL PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES menu_runs(id) ON DELETE CASCADE,
    college_id       INTEGER NOT NULL REFERENCES colleges(id),
    menu_date        DATE NOT NULL,             -- the actual date, computed in Python
    week_commencing  DATE
);

-- A meal sitting within a day.
CREATE TABLE meal_sittings (
    id           SERIAL PRIMARY KEY,
    day_menu_id  INTEGER NOT NULL REFERENCES day_menus(id) ON DELETE CASCADE,
    meal         TEXT NOT NULL                  -- 'breakfast'|'brunch'|'lunch'|'dinner'
);

-- A dish within a sitting.
CREATE TABLE dishes (
    id               SERIAL PRIMARY KEY,
    meal_sitting_id  INTEGER NOT NULL REFERENCES meal_sittings(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    course           TEXT,
    dietary          TEXT[]                     -- Postgres array: {'vegan','halal'}
);