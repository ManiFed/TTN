#!/usr/bin/env python3
"""
PostgreSQL persistence for the Boundless Skies cloud.

Reads DATABASE_URL from the environment (set by Railway / Fly.io).
The public API (init, connect, query, query_one, execute, executemany, loads)
is identical to the old SQLite version so callers don't need to change.

    from cloud import db
    db.init(url)           # url falls back to DATABASE_URL env var if empty
    db.query("SELECT …")
    db.execute("INSERT …", params)
"""

import json
import logging
import os
import threading
from typing import Any, Optional

import psycopg2
import psycopg2.errors
import psycopg2.extras

logger = logging.getLogger("cloud.db")

_DB_URL: Optional[str] = None
_init_lock = threading.Lock()

# Each element is one DDL statement (no trailing semicolon needed).
_SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS nodes (
        node_id                TEXT PRIMARY KEY,
        api_key                TEXT NOT NULL,
        owner_name             TEXT DEFAULT '',
        owner_email            TEXT DEFAULT '',
        latitude               DOUBLE PRECISION NOT NULL,
        longitude              DOUBLE PRECISION NOT NULL,
        elevation              DOUBLE PRECISION DEFAULT 0,
        city                   TEXT DEFAULT '',
        country                TEXT DEFAULT '',
        utc_offset_hours       DOUBLE PRECISION DEFAULT 0,
        light_pollution_mpsas  DOUBLE PRECISION DEFAULT 20.0,
        bortle                 INTEGER DEFAULT 5,
        horizon_mask           TEXT DEFAULT '[]',
        tier                   INTEGER DEFAULT 1,
        telescope_model        TEXT DEFAULT 'ZWO Seestar S50',
        aperture_mm            DOUBLE PRECISION DEFAULT 50,
        focal_length_mm        DOUBLE PRECISION DEFAULT 250,
        fov_deg                DOUBLE PRECISION DEFAULT 1.27,
        pixel_scale_arcsec     DOUBLE PRECISION DEFAULT 2.4,
        mount_type             TEXT DEFAULT 'alt_az',
        max_exposure_s         DOUBLE PRECISION DEFAULT 30.0,
        camera_model           TEXT DEFAULT '',
        cooled_camera          INTEGER DEFAULT 0,
        filter_set             TEXT DEFAULT '["CV"]',
        filters                TEXT DEFAULT 'CV',
        mag_bright_limit       DOUBLE PRECISION DEFAULT 6.0,
        mag_faint_limit        DOUBLE PRECISION DEFAULT 15.5,
        min_altitude_deg       DOUBLE PRECISION DEFAULT 25.0,
        has_dew_heater         INTEGER DEFAULT 0,
        has_power_mgmt         INTEGER DEFAULT 0,
        has_enclosure          INTEGER DEFAULT 0,
        has_ups                INTEGER DEFAULT 0,
        status                 TEXT DEFAULT 'active',
        registered_at          TEXT NOT NULL,
        last_heartbeat         TEXT,
        last_conditions        TEXT DEFAULT '{}',
        scheduling_notes       TEXT DEFAULT '',
        preferred_targets      TEXT DEFAULT '[]',
        total_observations     INTEGER DEFAULT 0,
        aavso_accepted         INTEGER DEFAULT 0,
        aavso_rejected         INTEGER DEFAULT 0,
        mean_uncertainty       DOUBLE PRECISION DEFAULT 0.0,
        mean_fwhm              DOUBLE PRECISION DEFAULT 0.0,
        clear_nights_30d       INTEGER DEFAULT 0,
        outlier_rate           DOUBLE PRECISION DEFAULT 0.0,
        reliability_score      DOUBLE PRECISION DEFAULT 0.5,
        perf_updated_at        TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS targets (
        target_id      TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        ra_deg         DOUBLE PRECISION NOT NULL,
        dec_deg        DOUBLE PRECISION NOT NULL,
        mag            DOUBLE PRECISION,
        mag_band       TEXT DEFAULT '',
        target_type    TEXT DEFAULT 'unknown',
        priority       DOUBLE PRECISION DEFAULT 0.5,
        time_critical  INTEGER DEFAULT 0,
        cadence_hours  DOUBLE PRECISION DEFAULT 24.0,
        sources        TEXT DEFAULT '[]',
        discovered_at  TEXT,
        last_updated   TEXT,
        active         INTEGER DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_targets_active ON targets(active)",
    "CREATE INDEX IF NOT EXISTS idx_targets_coords ON targets(ra_deg, dec_deg)",
    """
    CREATE TABLE IF NOT EXISTS scores (
        target_id      TEXT NOT NULL,
        node_id        TEXT NOT NULL,
        scored_at      TEXT NOT NULL,
        total          DOUBLE PRECISION NOT NULL,
        components     TEXT DEFAULT '{}',
        PRIMARY KEY (target_id, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        plan_id        TEXT PRIMARY KEY,
        node_id        TEXT NOT NULL,
        night          TEXT NOT NULL,
        generated_at   TEXT NOT NULL,
        plan_json      TEXT NOT NULL,
        status         TEXT DEFAULT 'current'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plans_node ON plans(node_id, status)",
    """
    CREATE TABLE IF NOT EXISTS measurements (
        id                 SERIAL PRIMARY KEY,
        node_id            TEXT NOT NULL,
        target_name        TEXT NOT NULL,
        bjd                DOUBLE PRECISION NOT NULL,
        magnitude          DOUBLE PRECISION NOT NULL,
        uncertainty        DOUBLE PRECISION NOT NULL,
        filter             TEXT DEFAULT 'CV',
        airmass            DOUBLE PRECISION,
        fwhm               DOUBLE PRECISION,
        snr                DOUBLE PRECISION,
        comparison_stars   INTEGER DEFAULT 0,
        quality_flag       TEXT DEFAULT 'poor',
        zero_point         DOUBLE PRECISION,
        zp_scatter         DOUBLE PRECISION,
        fits_file          TEXT DEFAULT '',
        conditions         TEXT DEFAULT '{}',
        received_at        TEXT NOT NULL,
        validation_status  TEXT DEFAULT 'unvalidated',
        aavso_submitted    INTEGER DEFAULT 0,
        UNIQUE (node_id, target_name, bjd, filter)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_meas_target ON measurements(target_name, bjd)",
    "CREATE INDEX IF NOT EXISTS idx_meas_pending ON measurements(aavso_submitted, validation_status, quality_flag)",
    """
    CREATE TABLE IF NOT EXISTS aavso_batches (
        id            SERIAL PRIMARY KEY,
        submitted_at  TEXT NOT NULL,
        file_path     TEXT,
        n_obs         INTEGER DEFAULT 0,
        status        TEXT DEFAULT 'pending',
        accepted      INTEGER DEFAULT 0,
        rejected      INTEGER DEFAULT 0,
        message       TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS interrupts (
        id            SERIAL PRIMARY KEY,
        target_id     TEXT,
        name          TEXT NOT NULL,
        ra_deg        DOUBLE PRECISION NOT NULL,
        dec_deg       DOUBLE PRECISION NOT NULL,
        mag           DOUBLE PRECISION,
        reason        TEXT DEFAULT '',
        node_ids      TEXT,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        acked_by      TEXT DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id         TEXT PRIMARY KEY,
        email           TEXT NOT NULL UNIQUE,
        password_hash   TEXT NOT NULL,
        salt            TEXT NOT NULL,
        auth_token_hash TEXT DEFAULT '',
        role            TEXT DEFAULT 'member',
        created_at      TEXT NOT NULL,
        last_login      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_users_token ON users(auth_token_hash)",
    """
    CREATE TABLE IF NOT EXISTS members (
        user_id             TEXT PRIMARY KEY REFERENCES users(user_id),
        display_name        TEXT DEFAULT '',
        country             TEXT DEFAULT '',
        notification_email  INTEGER DEFAULT 1,
        notification_push   INTEGER DEFAULT 1,
        push_token          TEXT DEFAULT '',
        created_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_members (
        node_id    TEXT NOT NULL,
        user_id    TEXT NOT NULL REFERENCES users(user_id),
        claimed_at TEXT NOT NULL,
        PRIMARY KEY (node_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS night_summaries (
        id             SERIAL PRIMARY KEY,
        node_id        TEXT NOT NULL,
        night          TEXT NOT NULL,
        n_targets      INTEGER DEFAULT 0,
        n_observations INTEGER DEFAULT 0,
        n_submitted    INTEGER DEFAULT 0,
        summary_json   TEXT NOT NULL DEFAULT '{}',
        generated_at   TEXT NOT NULL,
        sent_at        TEXT,
        UNIQUE (node_id, night)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_summaries_node ON night_summaries(node_id, night)",
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id        SERIAL PRIMARY KEY,
        user_id   TEXT NOT NULL REFERENCES users(user_id),
        type      TEXT NOT NULL,
        payload   TEXT DEFAULT '{}',
        sent_at   TEXT NOT NULL,
        read_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at)",
    """
    CREATE TABLE IF NOT EXISTS review_queue (
        id             SERIAL PRIMARY KEY,
        measurement_id INTEGER NOT NULL REFERENCES measurements(id),
        flagged_at     TEXT NOT NULL,
        reason         TEXT DEFAULT '',
        reviewer       TEXT DEFAULT '',
        reviewed_at    TEXT,
        decision       TEXT DEFAULT 'pending'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_review_pending ON review_queue(decision)",
    """
    CREATE TABLE IF NOT EXISTS tuning_state (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        obs_weights     TEXT NOT NULL DEFAULT '{}',
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS weight_history (
        id              SERIAL PRIMARY KEY,
        changed_at      TEXT NOT NULL,
        old_weights     TEXT NOT NULL DEFAULT '{}',
        new_weights     TEXT NOT NULL DEFAULT '{}',
        rationale       TEXT DEFAULT '',
        evidence_digest TEXT DEFAULT '{}',
        model           TEXT DEFAULT '',
        applied         INTEGER DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_weight_history_time ON weight_history(changed_at)",
    """
    CREATE TABLE IF NOT EXISTS activation_codes (
        code         TEXT PRIMARY KEY,
        user_id      TEXT REFERENCES users(user_id),
        node_id      TEXT DEFAULT '',
        created_at   TEXT NOT NULL,
        expires_at   TEXT,
        used_at      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_codes_user ON activation_codes(user_id)",
]

# Columns added after initial schema. init() applies these idempotently.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("nodes", "tier",               "INTEGER DEFAULT 1"),
    ("nodes", "camera_model",       "TEXT DEFAULT ''"),
    ("nodes", "mount_type",         "TEXT DEFAULT 'alt_az'"),
    ("nodes", "cooled_camera",      "INTEGER DEFAULT 0"),
    ("nodes", "filter_set",         "TEXT DEFAULT '[\"CV\"]'"),
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
    ("nodes", "mean_uncertainty",   "DOUBLE PRECISION DEFAULT 0.0"),
    ("nodes", "mean_fwhm",          "DOUBLE PRECISION DEFAULT 0.0"),
    ("nodes", "clear_nights_30d",   "INTEGER DEFAULT 0"),
    ("nodes", "outlier_rate",       "DOUBLE PRECISION DEFAULT 0.0"),
    ("nodes", "reliability_score",  "DOUBLE PRECISION DEFAULT 0.5"),
    ("nodes", "perf_updated_at",    "TEXT DEFAULT ''"),
]


def _run_migrations(conn) -> None:
    cur = conn.cursor()
    for table, col, defn in _COLUMN_MIGRATIONS:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, col),
        )
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {defn}")
            logger.info("Migration: added %s.%s", table, col)


def init(url: str = "") -> None:
    """Connect, create schema if missing, run column migrations."""
    global _DB_URL
    with _init_lock:
        _DB_URL = url or os.environ.get("DATABASE_URL", "")
        if not _DB_URL:
            raise RuntimeError(
                "No database URL configured. Set DATABASE_URL or pass url to db.init()."
            )
        conn = psycopg2.connect(_DB_URL)
        try:
            cur = conn.cursor()
            for stmt in _SCHEMA:
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            _run_migrations(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info("Database ready: %s", _DB_URL.split("@")[-1])


def connect():
    """Open a psycopg2 connection. Use as context manager for transactions;
    caller is responsible for closing."""
    if _DB_URL is None:
        raise RuntimeError("cloud.db.init() has not been called")
    return psycopg2.connect(_DB_URL)


# ── Convenience helpers ────────────────────────────────────────────────────────

def query(sql: str, params: tuple = ()) -> list:
    """Run a SELECT and return a list of plain dicts."""
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = (), returning_id: bool = False) -> int:
    """Run a single write statement.

    Pass returning_id=True when inserting into a table with a serial 'id'
    column and you need the new row's id back.
    """
    run_sql = sql
    if returning_id and "RETURNING" not in sql.upper():
        run_sql = sql.rstrip().rstrip(";") + " RETURNING id"
    conn = connect()
    try:
        with conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(run_sql, params)
            if returning_id:
                row = cur.fetchone()
                return row["id"] if row else 0
            return 0
    finally:
        conn.close()


def executemany(sql: str, seq: list) -> None:
    conn = connect()
    try:
        with conn:
            cur = conn.cursor()
            cur.executemany(sql, seq)
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
