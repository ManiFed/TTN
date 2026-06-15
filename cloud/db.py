#!/usr/bin/env python3
"""
SQLite persistence for the Boundless Skies cloud.

One file, WAL mode, per-call connections — safe across the Flask request
threads and the background ingestion/scoring/scheduling loops without an ORM.

    from cloud import db
    db.init(path)
    with db.connect() as conn:
        conn.execute(...)
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("cloud.db")

_DB_PATH: Optional[Path] = None
_init_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    -- Identity
    node_id                TEXT PRIMARY KEY,
    api_key                TEXT NOT NULL,
    owner_name             TEXT DEFAULT '',
    owner_email            TEXT DEFAULT '',

    -- Location (used for observability windows, airmass, twilight times)
    latitude               REAL NOT NULL,
    longitude              REAL NOT NULL,
    elevation              REAL DEFAULT 0,
    city                   TEXT DEFAULT '',
    country                TEXT DEFAULT '',
    utc_offset_hours       REAL DEFAULT 0,

    -- Sky quality at this location
    light_pollution_mpsas  REAL DEFAULT 20.0,
    bortle                 INTEGER DEFAULT 5,

    -- Local horizon obstructions: JSON [[alt_deg, az_deg], ...]
    -- Used by scheduler to avoid targets behind trees/buildings
    horizon_mask           TEXT DEFAULT '[]',

    -- Hardware: telescope
    tier                   INTEGER DEFAULT 1,      -- 1=Seestar, 2=Filtered, 3=Spectroscopy
    telescope_model        TEXT DEFAULT 'ZWO Seestar S50',
    aperture_mm            REAL DEFAULT 50,
    focal_length_mm        REAL DEFAULT 250,
    fov_deg                REAL DEFAULT 1.27,      -- diagonal, degrees
    pixel_scale_arcsec     REAL DEFAULT 2.4,
    mount_type             TEXT DEFAULT 'alt_az',  -- alt_az | equatorial
    max_exposure_s         REAL DEFAULT 30.0,      -- field rotation limit per sub

    -- Hardware: camera
    camera_model           TEXT DEFAULT '',
    cooled_camera          INTEGER DEFAULT 0,      -- 1 = TEC cooled (lower noise, fainter limit)

    -- Hardware: photometry capability
    filter_set             TEXT DEFAULT '["CV"]',  -- JSON list, e.g. ["B","V","R","I"]
    filters                TEXT DEFAULT 'CV',      -- legacy comma-separated, kept for compat
    mag_bright_limit       REAL DEFAULT 6.0,       -- saturates brighter than this
    mag_faint_limit        REAL DEFAULT 15.5,      -- SNR < threshold fainter than this
    min_altitude_deg       REAL DEFAULT 25.0,

    -- Hardware: autonomy (critical for overnight unattended operation)
    -- Each flag improves the node's ability to run without human intervention
    has_dew_heater         INTEGER DEFAULT 0,      -- prevents lens fogging in humid conditions
    has_power_mgmt         INTEGER DEFAULT 0,      -- smart power box: can remotely cycle Seestar
    has_enclosure          INTEGER DEFAULT 0,      -- dome/minidome: operates in light rain/wind
    has_ups                INTEGER DEFAULT 0,      -- survives brief power cuts

    -- Status and connectivity
    status                 TEXT DEFAULT 'active',  -- active | offline | disabled
    registered_at          TEXT NOT NULL,
    last_heartbeat         TEXT,
    last_conditions        TEXT DEFAULT '{}',      -- JSON snapshot from heartbeat

    -- Operator notes visible to the scheduler
    -- e.g. "south horizon blocked past az=200", "poor in high wind", "WiFi unstable"
    scheduling_notes       TEXT DEFAULT '',

    -- Target type preferences (JSON list): types this node is historically best at
    -- e.g. ["SN", "nova"] means prioritise these targets for this node
    preferred_targets      TEXT DEFAULT '[]',

    -- ── Performance metrics (recomputed nightly by the maintenance loop) ─────
    -- These are the ground truth for whether a node actually delivers science.
    -- The scheduler uses reliability_score as a final multiplier on all scores:
    -- a node that looks good on paper but consistently produces outlier or
    -- rejected data will be deprioritised.

    total_observations     INTEGER DEFAULT 0,      -- all-time measurement count
    aavso_accepted         INTEGER DEFAULT 0,      -- submitted and accepted by AAVSO
    aavso_rejected         INTEGER DEFAULT 0,      -- flagged as outlier (not submitted)
    mean_uncertainty       REAL DEFAULT 0.0,       -- avg photometric uncertainty (mag)
    mean_fwhm              REAL DEFAULT 0.0,       -- avg seeing (pixels, proxy for image quality)
    clear_nights_30d       INTEGER DEFAULT 0,      -- distinct nights with ≥1 obs in last 30 days
    outlier_rate           REAL DEFAULT 0.0,       -- fraction flagged as cross-val outlier (0..1)

    -- Composite reliability score (0..1) used as a multiplier by the scheduler.
    -- Formula: 0.40 × acceptance_rate + 0.25 × (1 − outlier_rate)
    --        + 0.20 × clear_fraction_30d + 0.15 × precision_factor
    -- New nodes start at 0.50 and converge toward their true value over ~20 observations.
    reliability_score      REAL DEFAULT 0.5,

    perf_updated_at        TEXT DEFAULT ''         -- ISO timestamp of last performance refresh
);

CREATE TABLE IF NOT EXISTS targets (
    target_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    ra_deg         REAL NOT NULL,
    dec_deg        REAL NOT NULL,
    mag            REAL,
    mag_band       TEXT DEFAULT '',
    target_type    TEXT DEFAULT 'unknown',
    priority       REAL DEFAULT 0.5,
    time_critical  INTEGER DEFAULT 0,
    cadence_hours  REAL DEFAULT 24.0,
    sources        TEXT DEFAULT '[]',           -- JSON list
    discovered_at  TEXT,
    last_updated   TEXT,
    active         INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_targets_active ON targets(active);
CREATE INDEX IF NOT EXISTS idx_targets_coords ON targets(ra_deg, dec_deg);

CREATE TABLE IF NOT EXISTS scores (
    target_id      TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    scored_at      TEXT NOT NULL,
    total          REAL NOT NULL,
    components     TEXT DEFAULT '{}',           -- JSON breakdown
    PRIMARY KEY (target_id, node_id)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id        TEXT PRIMARY KEY,
    node_id        TEXT NOT NULL,
    night          TEXT NOT NULL,               -- YYYY-MM-DD local evening
    generated_at   TEXT NOT NULL,
    plan_json      TEXT NOT NULL,               -- full ObservationPlan dict
    status         TEXT DEFAULT 'current'       -- current | superseded
);
CREATE INDEX IF NOT EXISTS idx_plans_node ON plans(node_id, status);

CREATE TABLE IF NOT EXISTS measurements (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id            TEXT NOT NULL,
    target_name        TEXT NOT NULL,
    bjd                REAL NOT NULL,
    magnitude          REAL NOT NULL,
    uncertainty        REAL NOT NULL,
    filter             TEXT DEFAULT 'CV',
    airmass            REAL,
    fwhm               REAL,
    snr                REAL,
    comparison_stars   INTEGER DEFAULT 0,
    quality_flag       TEXT DEFAULT 'poor',
    zero_point         REAL,
    zp_scatter         REAL,
    fits_file          TEXT DEFAULT '',
    conditions         TEXT DEFAULT '{}',       -- node-local conditions JSON
    received_at        TEXT NOT NULL,
    validation_status  TEXT DEFAULT 'unvalidated',  -- unvalidated|consistent|outlier|single
    aavso_submitted    INTEGER DEFAULT 0,
    UNIQUE (node_id, target_name, bjd, filter)
);
CREATE INDEX IF NOT EXISTS idx_meas_target ON measurements(target_name, bjd);
CREATE INDEX IF NOT EXISTS idx_meas_pending
    ON measurements(aavso_submitted, validation_status, quality_flag);

CREATE TABLE IF NOT EXISTS aavso_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at  TEXT NOT NULL,
    file_path     TEXT,
    n_obs         INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'pending',       -- accepted|rejected|error|dry_run
    accepted      INTEGER DEFAULT 0,
    rejected      INTEGER DEFAULT 0,
    message       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS interrupts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id     TEXT,
    name          TEXT NOT NULL,
    ra_deg        REAL NOT NULL,
    dec_deg       REAL NOT NULL,
    mag           REAL,
    reason        TEXT DEFAULT '',
    node_ids      TEXT,                          -- JSON list, NULL = all nodes
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    acked_by      TEXT DEFAULT '[]'              -- JSON list of node_ids
);

CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    salt            TEXT NOT NULL,
    auth_token_hash TEXT DEFAULT '',
    role            TEXT DEFAULT 'member',       -- member | admin
    created_at      TEXT NOT NULL,
    last_login      TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_token ON users(auth_token_hash);

CREATE TABLE IF NOT EXISTS members (
    user_id             TEXT PRIMARY KEY REFERENCES users(user_id),
    display_name        TEXT DEFAULT '',
    country             TEXT DEFAULT '',
    notification_email  INTEGER DEFAULT 1,
    notification_push   INTEGER DEFAULT 1,
    push_token          TEXT DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_members (
    node_id    TEXT NOT NULL,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (node_id, user_id)
);

CREATE TABLE IF NOT EXISTS night_summaries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id        TEXT NOT NULL,
    night          TEXT NOT NULL,
    n_targets      INTEGER DEFAULT 0,
    n_observations INTEGER DEFAULT 0,
    n_submitted    INTEGER DEFAULT 0,
    summary_json   TEXT NOT NULL DEFAULT '{}',  -- per-target detail
    generated_at   TEXT NOT NULL,
    sent_at        TEXT,
    UNIQUE (node_id, night)
);
CREATE INDEX IF NOT EXISTS idx_summaries_node ON night_summaries(node_id, night);

CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT NOT NULL REFERENCES users(user_id),
    type      TEXT NOT NULL,
    payload   TEXT DEFAULT '{}',
    sent_at   TEXT NOT NULL,
    read_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at);

CREATE TABLE IF NOT EXISTS review_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id INTEGER NOT NULL REFERENCES measurements(id),
    flagged_at     TEXT NOT NULL,
    reason         TEXT DEFAULT '',
    reviewer       TEXT DEFAULT '',
    reviewed_at    TEXT,
    decision       TEXT DEFAULT 'pending'        -- pending | accept | reject
);
CREATE INDEX IF NOT EXISTS idx_review_pending ON review_queue(decision);

-- ── Scoring weight tuning ───────────────────────────────────────────────────
-- The observability sub-weights are the one part of the scoring formula that is
-- auto-tuned (nightly) by the Claude monitor in cloud/tuning.py.  The scoring
-- engine reads the *active* weights from here on every run, so changes take
-- effect without a process restart.  `config.yaml` is only the seed/default.
CREATE TABLE IF NOT EXISTS tuning_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- single active row
    obs_weights     TEXT NOT NULL DEFAULT '{}',          -- JSON: the 6 sub-weights
    updated_at      TEXT NOT NULL
);

-- Append-only audit of every weight change (auto-applied, but fully traceable
-- and reversible — see /api/v1/admin/tuning/rollback).
CREATE TABLE IF NOT EXISTS weight_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at      TEXT NOT NULL,
    old_weights     TEXT NOT NULL DEFAULT '{}',          -- JSON
    new_weights     TEXT NOT NULL DEFAULT '{}',          -- JSON (clamped + normalized)
    rationale       TEXT DEFAULT '',                     -- Claude's explanation
    evidence_digest TEXT DEFAULT '{}',                   -- JSON brief the decision was based on
    model           TEXT DEFAULT '',                     -- model id used
    applied         INTEGER DEFAULT 1                    -- 1 = applied, 0 = proposed only
);
CREATE INDEX IF NOT EXISTS idx_weight_history_time ON weight_history(changed_at);

CREATE TABLE IF NOT EXISTS activation_codes (
    code         TEXT PRIMARY KEY,
    user_id      TEXT REFERENCES users(user_id), -- NULL = generic (not pre-linked)
    node_id      TEXT DEFAULT '',               -- set when consumed
    created_at   TEXT NOT NULL,
    expires_at   TEXT,                          -- NULL = no expiry
    used_at      TEXT                           -- NULL = not yet used
);
CREATE INDEX IF NOT EXISTS idx_codes_user ON activation_codes(user_id);
"""


# New columns added to existing tables.  Appended to here whenever the schema
# grows; init() runs them once against every existing database so no manual
# migration step is ever needed.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column_name, column_definition)
    ("nodes", "tier",               "INTEGER DEFAULT 1"),
    ("nodes", "camera_model",       "TEXT DEFAULT ''"),
    ("nodes", "mount_type",         "TEXT DEFAULT 'alt_az'"),
    ("nodes", "cooled_camera",      "INTEGER DEFAULT 0"),
    ("nodes", "filter_set",         'TEXT DEFAULT \'["CV"]\''),
    ("nodes", "filters",            "TEXT DEFAULT 'CV'"),
    ("nodes", "horizon_mask",       "TEXT DEFAULT '[]'"),
    ("nodes", "has_dew_heater",     "INTEGER DEFAULT 0"),
    ("nodes", "has_power_mgmt",     "INTEGER DEFAULT 0"),
    ("nodes", "has_enclosure",      "INTEGER DEFAULT 0"),
    ("nodes", "has_ups",            "INTEGER DEFAULT 0"),
    ("nodes", "scheduling_notes",   "TEXT DEFAULT ''"),
    ("nodes", "preferred_targets",  "TEXT DEFAULT '[]'"),
    ("nodes", "total_observations", "INTEGER DEFAULT 0"),
    ("nodes", "aavso_accepted",     "INTEGER DEFAULT 0"),
    ("nodes", "aavso_rejected",     "INTEGER DEFAULT 0"),
    ("nodes", "mean_uncertainty",   "REAL DEFAULT 0.0"),
    ("nodes", "mean_fwhm",          "REAL DEFAULT 0.0"),
    ("nodes", "clear_nights_30d",   "INTEGER DEFAULT 0"),
    ("nodes", "outlier_rate",       "REAL DEFAULT 0.0"),
    ("nodes", "reliability_score",  "REAL DEFAULT 0.5"),
    ("nodes", "perf_updated_at",    "TEXT DEFAULT ''"),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Add any missing columns to existing tables.  Safe to call on every init."""
    existing: dict[str, set] = {}
    for table, col, defn in _COLUMN_MIGRATIONS:
        if table not in existing:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            existing[table] = {r[1] for r in rows}
        if col not in existing[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            existing[table].add(col)
            logger.info("Migration: added %s.%s", table, col)


def init(path: str = "cloud_data/cloud.db") -> None:
    """Create the database file and schema if missing, then run column migrations."""
    global _DB_PATH
    with _init_lock:
        _DB_PATH = Path(path)
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            _run_migrations(conn)
            conn.commit()
        finally:
            conn.close()
        logger.info("Database ready: %s", _DB_PATH)


def connect() -> sqlite3.Connection:
    """Open a connection with row access by column name. Use as context manager
    for transactions; caller is responsible for closing (or use query helpers)."""
    if _DB_PATH is None:
        raise RuntimeError("cloud.db.init() has not been called")
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Convenience helpers ────────────────────────────────────────────────────────

def query(sql: str, params: tuple = ()) -> list:
    """Run a SELECT and return a list of plain dicts."""
    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    """Run a single write statement. Returns lastrowid."""
    conn = connect()
    try:
        with conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or 0
    finally:
        conn.close()


def executemany(sql: str, seq: list) -> None:
    conn = connect()
    try:
        with conn:
            conn.executemany(sql, seq)
    finally:
        conn.close()


def loads(text: Any, default: Any = None) -> Any:
    """Tolerant JSON column decoder."""
    if not text:
        return default if default is not None else {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default if default is not None else {}
