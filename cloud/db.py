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
    node_id                TEXT PRIMARY KEY,
    api_key                TEXT NOT NULL,
    owner_name             TEXT DEFAULT '',
    owner_email            TEXT DEFAULT '',
    latitude               REAL NOT NULL,
    longitude              REAL NOT NULL,
    elevation              REAL DEFAULT 0,
    city                   TEXT DEFAULT '',
    country                TEXT DEFAULT '',
    utc_offset_hours       REAL DEFAULT 0,
    telescope_model        TEXT DEFAULT 'ZWO Seestar S50',
    aperture_mm            REAL DEFAULT 50,
    focal_length_mm        REAL DEFAULT 250,
    fov_deg                REAL DEFAULT 1.27,
    pixel_scale_arcsec     REAL DEFAULT 2.4,
    filters                TEXT DEFAULT 'CV',
    mag_bright_limit       REAL DEFAULT 6.0,
    mag_faint_limit        REAL DEFAULT 15.5,
    min_altitude_deg       REAL DEFAULT 25.0,
    max_exposure_s         REAL DEFAULT 30.0,
    light_pollution_mpsas  REAL DEFAULT 20.0,
    bortle                 INTEGER DEFAULT 5,
    status                 TEXT DEFAULT 'active',
    registered_at          TEXT NOT NULL,
    last_heartbeat         TEXT,
    last_conditions        TEXT DEFAULT '{}'    -- JSON from heartbeat
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
"""


def init(path: str = "cloud_data/cloud.db") -> None:
    """Create the database file and schema if missing."""
    global _DB_PATH
    with _init_lock:
        _DB_PATH = Path(path)
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
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
