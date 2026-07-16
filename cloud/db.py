#!/usr/bin/env python3
"""
PostgreSQL persistence for the The Telescope Net cloud.

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
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("cloud.db")

_DB_URL: Optional[str] = None
_pool: Optional[ThreadedConnectionPool] = None
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
        scheduler_trust_score  DOUBLE PRECISION DEFAULT 0.5,
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
    # Multiple concurrent sessions per user (desktop + mobile signed in at
    # once, etc). Previously login overwrote users.auth_token_hash, silently
    # killing every other device's session the moment one signed back in --
    # that column is unused now but kept for backward compat with old rows.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash   TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL REFERENCES users(user_id),
        created_at   TEXT NOT NULL,
        last_used_at TEXT NOT NULL,
        expires_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
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
    CREATE TABLE IF NOT EXISTS reliability_incidents (
        id             SERIAL PRIMARY KEY,
        node_id        TEXT NOT NULL,
        incident_type  TEXT NOT NULL,
        severity       TEXT DEFAULT 'info',
        target_name    TEXT DEFAULT '',
        measurement_id INTEGER,
        detail         TEXT DEFAULT '{}',
        occurred_at    TEXT NOT NULL,
        resolved_at    TEXT
    )
    """,
    "ALTER TABLE reliability_incidents ADD COLUMN IF NOT EXISTS idempotency_key TEXT DEFAULT ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reliability_incident_idempotency "
    "ON reliability_incidents(node_id,idempotency_key) WHERE idempotency_key<>''",
    "CREATE INDEX IF NOT EXISTS idx_incidents_node_time ON reliability_incidents(node_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_incidents_open ON reliability_incidents(node_id, resolved_at)",
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
        code             TEXT PRIMARY KEY,
        user_id          TEXT REFERENCES users(user_id),
        node_id          TEXT DEFAULT '',
        created_at       TEXT NOT NULL,
        expires_at       TEXT,
        used_at          TEXT,
        observatory_name TEXT DEFAULT '',
        latitude         DOUBLE PRECISION,
        longitude        DOUBLE PRECISION,
        telescope_model  TEXT DEFAULT '',
        telescope_specs  TEXT DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_codes_user ON activation_codes(user_id)",
    "ALTER TABLE activation_codes ADD COLUMN IF NOT EXISTS observatory_name TEXT DEFAULT ''",
    "ALTER TABLE activation_codes ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION",
    "ALTER TABLE activation_codes ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION",
    "ALTER TABLE activation_codes ADD COLUMN IF NOT EXISTS telescope_model TEXT DEFAULT ''",
    "ALTER TABLE activation_codes ADD COLUMN IF NOT EXISTS telescope_specs TEXT DEFAULT '{}'",
    """
    CREATE TABLE IF NOT EXISTS site_config (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        member_count    INTEGER NOT NULL DEFAULT 7,
        updated_at      TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscribers (
        id               SERIAL PRIMARY KEY,
        email            TEXT NOT NULL,
        source           TEXT DEFAULT 'tour',
        equipment        TEXT DEFAULT '',
        subscribed_at    TEXT NOT NULL,
        activation_code  TEXT DEFAULT '',
        status           TEXT DEFAULT 'pending'
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers(email)",
    """
    CREATE TABLE IF NOT EXISTS transit_ephemerides (
        target_id      TEXT PRIMARY KEY,
        period_days    DOUBLE PRECISION NOT NULL,
        epoch_bjd      DOUBLE PRECISION NOT NULL,
        duration_hours DOUBLE PRECISION NOT NULL,
        depth_ppt      DOUBLE PRECISION DEFAULT 0,
        updated_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS patrol_detections (
        id              SERIAL PRIMARY KEY,
        measurement_id  INTEGER REFERENCES measurements(id),
        node_id         TEXT NOT NULL,
        target_name     TEXT NOT NULL,
        bjd             DOUBLE PRECISION NOT NULL,
        ra_deg          DOUBLE PRECISION NOT NULL,
        dec_deg         DOUBLE PRECISION NOT NULL,
        est_mag         DOUBLE PRECISION,
        catalog_mag     DOUBLE PRECISION,
        delta_mag       DOUBLE PRECISION,
        alert_type      TEXT NOT NULL,
        status          TEXT DEFAULT 'new',
        detected_at     TEXT NOT NULL,
        UNIQUE (node_id, bjd, ra_deg, dec_deg)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_patrol_time ON patrol_detections(detected_at)",
    "CREATE INDEX IF NOT EXISTS idx_patrol_status ON patrol_detections(status, detected_at)",
]

# Seed statements run once after schema creation (idempotent via ON CONFLICT DO NOTHING).
_SEEDS: list[str] = [
    "INSERT INTO site_config (id, member_count, updated_at) VALUES (1, 7, '') ON CONFLICT (id) DO NOTHING",
]

# Columns added after initial schema. init() applies these idempotently.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("nodes", "tier",               "INTEGER DEFAULT 1"),
    ("nodes", "telescope_serial",   "TEXT DEFAULT ''"),
    ("nodes", "telescope_name",     "TEXT DEFAULT ''"),
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
    ("nodes", "scheduler_trust_score", "DOUBLE PRECISION DEFAULT 0.5"),
    ("nodes", "perf_updated_at",      "TEXT DEFAULT ''"),
    ("nodes", "portable",             "INTEGER DEFAULT 0"),
    ("nodes", "vacation_until",       "TEXT DEFAULT ''"),
    ("nodes", "vacation_from",        "TEXT DEFAULT ''"),
    ("nodes", "session_lat",          "DOUBLE PRECISION DEFAULT 0"),
    ("nodes", "session_lon",          "DOUBLE PRECISION DEFAULT 0"),
    ("nodes", "session_city",         "TEXT DEFAULT ''"),
    ("nodes", "session_site_name",    "TEXT DEFAULT ''"),
    ("nodes", "previous_locations",   "TEXT DEFAULT '[]'"),
    ("activation_codes", "portable",  "INTEGER DEFAULT 0"),
    ("activation_codes", "telescope_display_name", "TEXT DEFAULT ''"),
    ("node_members", "display_name", "TEXT DEFAULT ''"),
    ("measurements",     "sky_mag",   "DOUBLE PRECISION"),
    ("measurements", "item_id", "TEXT DEFAULT ''"),
    ("measurements", "bundle_id", "TEXT DEFAULT ''"),
    ("measurements", "response_fingerprint", "TEXT DEFAULT ''"),
    ("measurements", "instrumental_magnitude", "DOUBLE PRECISION"),
    ("measurements", "network_magnitude", "DOUBLE PRECISION"),
    ("measurements", "network_uncertainty", "DOUBLE PRECISION"),
    ("measurements", "calibration_correction", "DOUBLE PRECISION"),
    ("measurements", "calibration_model_version", "TEXT DEFAULT ''"),
    ("measurements", "calibration_state", "TEXT DEFAULT ''"),
    ("measurements", "magnitude_system", "TEXT DEFAULT ''"),
    ("nodes", "clock_skew_s", "DOUBLE PRECISION"),
    ("nodes", "clock_qualified_at", "TEXT DEFAULT ''"),
    ("calibration_samples", "catalog_band", "TEXT DEFAULT ''"),
    ("execution_outcomes", "last_checkpoint", "TEXT DEFAULT ''"),
    ("autonomy_bundles", "reconciliation", "TEXT DEFAULT '{}'"),
    # Network optimizer: all AI-tuned parameter groups live in one JSON blob,
    # superseding the observability-only tuning_state.obs_weights column.
    ("tuning_state",     "params",    "TEXT NOT NULL DEFAULT '{}'"),
    # Open Aperture: contributor activation codes create tier-0 virtual nodes
    # that upload survey frames but are never scheduled.
    ("activation_codes", "code_type", "TEXT DEFAULT 'node'"),
    # Self-characterization provenance: which capability columns hold values
    # the node *measured* from its own plate solves (vs. spec-sheet entries).
    ("nodes", "measured_specs", "TEXT DEFAULT '{}'"),
    ("nodes", "measured_at",    "TEXT DEFAULT ''"),
    # Universal frame ingestion. Contributions are no longer
    # required to arrive with a WCS — the cloud solver adds one. These columns
    # carry the staged pipeline's progress and results.
    ("contributions", "stage",              "TEXT DEFAULT ''"),      # triage|solve|extract|ingest
    ("contributions", "solved",             "INTEGER DEFAULT 0"),
    ("contributions", "pixel_scale_arcsec", "DOUBLE PRECISION"),
    ("contributions", "date_obs",           "TEXT DEFAULT ''"),
    ("contributions", "storage_key",        "TEXT DEFAULT ''"),      # object-store key
    ("contributions", "triage",             "TEXT DEFAULT '{}'"),
    ("contributions", "historical",         "INTEGER DEFAULT 0"),
    # Provenance: which human/contribution produced a survey measurement, so a
    # confirmed discovery can credit the person who caught it.
    ("survey_measurements", "contribution_id", "INTEGER"),
    ("survey_measurements", "user_id",         "TEXT DEFAULT ''"),
    ("survey_measurements", "task_id",         "TEXT DEFAULT ''"),
    ("survey_measurements", "event_id",        "TEXT DEFAULT ''"),
    ("survey_measurements", "event_revision",  "INTEGER DEFAULT 0"),
    ("observation_tasks", "result",             "TEXT DEFAULT '{}'"),
    ("calibration_samples", "response_family",  "TEXT DEFAULT ''"),
    ("photometric_models", "response_family",   "TEXT DEFAULT ''"),
    ("event_revisions", "notice_hash",           "TEXT DEFAULT ''"),
    # Moving-object linking: retire unlinked detections once their time window
    # has closed so the linker's scan set can't be starved by permanent noise.
    ("moving_object_detections", "link_done", "BOOLEAN DEFAULT FALSE"),
    # Comet coma heuristic: whether this detection's PSF was flagged extended
    # relative to the frame's stellar sharpness baseline (src/photometry.py).
    ("moving_object_detections", "extended", "BOOLEAN DEFAULT FALSE"),
    # NEO fast-mover flag and comet-vs-asteroid classification, both set when
    # a tracklet is linked/merged (cloud/moving_objects.py).
    ("asteroid_candidates", "priority", "TEXT DEFAULT 'normal'"),
    ("asteroid_candidates", "object_type", "TEXT DEFAULT 'asteroid'"),
    # How many consecutive prior nights the dropped node had already gone
    # dark for, recorded at dispatch time (cloud/chorus/reflow.py) so a
    # chronic dropout is distinguishable from a one-off cloud-out in the
    # audit trail.
    ("reflow_log", "dark_streak", "INTEGER DEFAULT 0"),
    # SNR-weighted posterior probability this candidate is real, combining
    # every reflex-triggered confirmation instead of the raw n_nodes/
    # n_detections counts alone (cloud/survey.py::_posterior_confidence).
    ("discovery_candidates", "confidence", "DOUBLE PRECISION DEFAULT 0.0"),
]

# Tables added after initial schema — created idempotently in init().
_LATE_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS member_highlights (
        id              SERIAL PRIMARY KEY,
        user_id         TEXT NOT NULL REFERENCES users(user_id),
        node_id         TEXT NOT NULL,
        measurement_id  INTEGER REFERENCES measurements(id),
        target_name     TEXT NOT NULL,
        target_type     TEXT DEFAULT '',
        bjd             DOUBLE PRECISION NOT NULL,
        magnitude       DOUBLE PRECISION NOT NULL,
        headline        TEXT NOT NULL,
        detail          TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        read_at         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_highlights_user ON member_highlights(user_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS incidents (
        id              SERIAL PRIMARY KEY,
        node_id         TEXT NOT NULL,
        status          TEXT DEFAULT 'open',
        title           TEXT NOT NULL,
        root_cause      TEXT DEFAULT 'unknown',
        severity        TEXT DEFAULT 'warning',
        opened_at       TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        resolved_at     TEXT,
        resolver        TEXT DEFAULT '',
        resolution_note TEXT DEFAULT '',
        trigger_event   TEXT DEFAULT '',
        n_raw_events    INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_incidents_node ON incidents(node_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents(status, opened_at)",
    """
    CREATE TABLE IF NOT EXISTS plan_runs (
        run_id           TEXT PRIMARY KEY,
        ran_at           TEXT NOT NULL,
        n_nodes          INTEGER DEFAULT 0,
        n_targets        INTEGER DEFAULT 0,
        n_assignments    INTEGER DEFAULT 0,
        objective_value  DOUBLE PRECISION DEFAULT 0,
        greedy_objective DOUBLE PRECISION DEFAULT 0,
        redundancy_rate  DOUBLE PRECISION DEFAULT 0,
        cadence_fill     DOUBLE PRECISION DEFAULT 0,
        stats            TEXT DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plan_runs_time ON plan_runs(ran_at)",
    """
    CREATE TABLE IF NOT EXISTS help_chat_messages (
        id           SERIAL PRIMARY KEY,
        user_id      TEXT NOT NULL REFERENCES users(user_id),
        role         TEXT NOT NULL,
        content      TEXT NOT NULL,
        config_patch TEXT DEFAULT '{}',
        node_id      TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_help_chat_user_time ON help_chat_messages(user_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS node_config_patches (
        id         SERIAL PRIMARY KEY,
        node_id    TEXT NOT NULL,
        user_id    TEXT NOT NULL REFERENCES users(user_id),
        patch_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        applied_at TEXT,
        status     TEXT DEFAULT 'pending',
        error      TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_config_patches_node ON node_config_patches(node_id, status)",
    """
    CREATE TABLE IF NOT EXISTS chorus_node_ledger (
        node_id     TEXT PRIMARY KEY,
        p_exec_a    DOUBLE PRECISION DEFAULT 4.0,
        p_exec_b    DOUBLE PRECISION DEFAULT 2.0,
        p_accept_a  DOUBLE PRECISION DEFAULT 4.0,
        p_accept_b  DOUBLE PRECISION DEFAULT 2.0,
        kappa       DOUBLE PRECISION DEFAULT 1.0,
        n_kappa     INTEGER DEFAULT 0,
        detail      TEXT DEFAULT '{}',
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chorus_target_state (
        target_id  TEXT PRIMARY KEY,
        state      TEXT DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chorus_site_calibration (
        node_id     TEXT PRIMARY KEY,
        a           DOUBLE PRECISION DEFAULT 0.0,
        b           DOUBLE PRECISION DEFAULT 1.0,
        n_nights    INTEGER DEFAULT 0,
        climatology TEXT DEFAULT '{}',
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chorus_run_archive (
        run_id       TEXT PRIMARY KEY,
        night        TEXT NOT NULL,
        ran_at       TEXT NOT NULL,
        inputs       TEXT NOT NULL,
        realized     TEXT DEFAULT '',
        phi_expected DOUBLE PRECISION DEFAULT 0,
        phi_realized DOUBLE PRECISION,
        shadow       INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chorus_archive_night ON chorus_run_archive(night)",
    # CHORUS Ring 2 — structural evolution (CHORUS.md §7): declarative,
    # per-family cell-generation knobs (cloud/chorus/ring2.py) that cells.py
    # reads instead of the hardcoded literals it used to fall back to. One
    # row per proposal/revision; only the newest 'live' row per family is
    # active (cloud.chorus.ring2.active_templates).
    """
    CREATE TABLE IF NOT EXISTS class_templates (
        id              SERIAL PRIMARY KEY,
        family          TEXT NOT NULL,
        params          TEXT DEFAULT '{}',
        stage           TEXT DEFAULT 'advisory',
        note            TEXT DEFAULT '',
        backtest_detail TEXT DEFAULT '{}',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_class_templates_family_stage "
    "ON class_templates(family, stage, id)",
    """
    CREATE TABLE IF NOT EXISTS science_program_suggestions (
        id               SERIAL PRIMARY KEY,
        user_id          TEXT NOT NULL REFERENCES users(user_id),
        email            TEXT NOT NULL DEFAULT '',
        title            TEXT NOT NULL,
        description      TEXT NOT NULL,
        target_examples  TEXT DEFAULT '',
        notes            TEXT DEFAULT '',
        status           TEXT DEFAULT 'pending',
        created_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_science_suggestions_time ON science_program_suggestions(created_at)",
    # ── Open Aperture: full-frame survey store ────────────────────────────────
    # One row per (star, filter): running Welford aggregates are the permanent
    # science product; raw survey_measurements carry a retention window.
    """
    CREATE TABLE IF NOT EXISTS survey_sources (
        source_key    TEXT NOT NULL,
        filter        TEXT NOT NULL DEFAULT 'CV',
        ra_deg        DOUBLE PRECISION NOT NULL,
        dec_deg       DOUBLE PRECISION NOT NULL,
        catalog_mag   DOUBLE PRECISION,
        catalog_err   DOUBLE PRECISION,
        catalog_src   TEXT DEFAULT '',
        n_obs         INTEGER DEFAULT 0,
        mean_mag      DOUBLE PRECISION,
        m2            DOUBLE PRECISION DEFAULT 0,
        last_bjd      DOUBLE PRECISION,
        last_mag      DOUBLE PRECISION,
        vsx_name      TEXT DEFAULT '',
        vsx_checked_at TEXT DEFAULT '',
        variability_flag TEXT DEFAULT 'none',
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (source_key, filter)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_survey_sources_coords ON survey_sources(ra_deg, dec_deg)",
    """
    CREATE TABLE IF NOT EXISTS survey_measurements (
        id          BIGSERIAL PRIMARY KEY,
        source_key  TEXT NOT NULL,
        node_id     TEXT NOT NULL,
        bjd         DOUBLE PRECISION NOT NULL,
        mag         DOUBLE PRECISION NOT NULL,
        mag_err     DOUBLE PRECISION,
        snr         DOUBLE PRECISION,
        filter      TEXT DEFAULT 'CV',
        frame_id    TEXT DEFAULT '',
        task_id     TEXT DEFAULT '',
        event_id    TEXT DEFAULT '',
        event_revision INTEGER DEFAULT 0,
        received_at TEXT NOT NULL,
        UNIQUE (source_key, node_id, bjd)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_survey_meas_source ON survey_measurements(source_key, bjd)",
    "CREATE INDEX IF NOT EXISTS idx_survey_meas_received ON survey_measurements USING BRIN (received_at)",
    """
    CREATE TABLE IF NOT EXISTS discovery_candidates (
        id             SERIAL PRIMARY KEY,
        source_key     TEXT NOT NULL,
        ra_deg         DOUBLE PRECISION NOT NULL,
        dec_deg        DOUBLE PRECISION NOT NULL,
        kind           TEXT NOT NULL,
        filter         TEXT DEFAULT 'CV',
        first_bjd      DOUBLE PRECISION,
        last_bjd       DOUBLE PRECISION,
        n_detections   INTEGER DEFAULT 1,
        n_nodes        INTEGER DEFAULT 1,
        node_ids       TEXT DEFAULT '[]',
        peak_delta_mag DOUBLE PRECISION,
        last_mag       DOUBLE PRECISION,
        state          TEXT DEFAULT 'detected',
        vsx_name       TEXT DEFAULT '',
        tns_name       TEXT DEFAULT '',
        target_id      TEXT DEFAULT '',
        detail         TEXT DEFAULT '{}',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_candidates_state ON discovery_candidates(state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_source ON discovery_candidates(source_key)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_coords ON discovery_candidates(ra_deg, dec_deg)",
    """
    CREATE TABLE IF NOT EXISTS contributions (
        id           SERIAL PRIMARY KEY,
        user_id      TEXT NOT NULL REFERENCES users(user_id),
        node_id      TEXT NOT NULL,
        filename     TEXT NOT NULL,
        sha256       TEXT NOT NULL UNIQUE,
        size_bytes   INTEGER DEFAULT 0,
        status       TEXT DEFAULT 'pending',
        wcs_present  INTEGER DEFAULT 0,
        n_sources    INTEGER DEFAULT 0,
        error        TEXT DEFAULT '',
        stored_path  TEXT DEFAULT '',
        created_at   TEXT NOT NULL,
        processed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_contributions_status ON contributions(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_contributions_user ON contributions(user_id, created_at)",
    # ── Live fleet state + server→node dispatch bus ────────────────────────────
    # node_live_state carries second-scale phase for every node (one row/node,
    # upserted on each heartbeat). 'offline' is derived at read time from
    # updated_at age, never written by a reaper.
    """
    CREATE TABLE IF NOT EXISTS node_live_state (
        node_id        TEXT PRIMARY KEY,
        phase          TEXT DEFAULT 'idle',
        target_name    TEXT DEFAULT '',
        plan_item_idx  INTEGER,
        exposure_ends_at TEXT DEFAULT '',
        sky_clear      DOUBLE PRECISION,
        is_dark        INTEGER DEFAULT 0,
        heartbeat_s    DOUBLE PRECISION DEFAULT 60,
        updated_at     TEXT NOT NULL,
        detail         TEXT DEFAULT '{}'
    )
    """,
    # node_night_utilization accrues per-(node, night) dark-time seconds from
    # successive heartbeats: how much dark time was spent observing vs idle vs
    # clouded. Written by live.record_state, read by the ledger's nightly
    # summary — the ground truth for "is the network wasting telescope time".
    """
    CREATE TABLE IF NOT EXISTS node_night_utilization (
        node_id      TEXT NOT NULL,
        night        TEXT NOT NULL,
        dark_s       DOUBLE PRECISION DEFAULT 0,
        observing_s  DOUBLE PRECISION DEFAULT 0,
        idle_s       DOUBLE PRECISION DEFAULT 0,
        clouded_s    DOUBLE PRECISION DEFAULT 0,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (node_id, night)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_night_util ON node_night_utilization(night)",
    # dispatch_events is the append-only push log the realtime SSE service tails
    # (via LISTEN/NOTIFY) and replays from on Last-Event-ID reconnect. Rows are
    # short-lived signals ("wake up and fetch"), pruned by the maintenance loop.
    """
    CREATE TABLE IF NOT EXISTS dispatch_events (
        id          BIGSERIAL PRIMARY KEY,
        node_id     TEXT NOT NULL,
        kind        TEXT NOT NULL,
        payload     TEXT DEFAULT '{}',
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dispatch_node ON dispatch_events(node_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_dispatch_created ON dispatch_events USING BRIN (created_at)",
    # reflow_log: audit of mid-night work reassignments (dropped node → new
    # node), one row per reflow. outcome is filled by the nightly ledger join.
    """
    CREATE TABLE IF NOT EXISTS reflow_log (
        id            SERIAL PRIMARY KEY,
        night         TEXT NOT NULL,
        from_node     TEXT NOT NULL,
        to_node       TEXT NOT NULL,
        target_id     TEXT NOT NULL,
        target_name   TEXT DEFAULT '',
        expected_info DOUBLE PRECISION DEFAULT 0,
        interrupt_id  INTEGER,
        reason        TEXT DEFAULT 'dropout',
        outcome       TEXT DEFAULT 'dispatched',
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reflow_night ON reflow_log(night)",
    # Universal frame ingestion: retrospective discovery. Deviations found in
    # historical (archive) frames land here, NOT in the live candidate flow —
    # a nova in a 2023 image is a real find but must never fire a live interrupt
    # or pollute the baseline it is compared against.
    """
    CREATE TABLE IF NOT EXISTS retro_discoveries (
        id             SERIAL PRIMARY KEY,
        source_key     TEXT NOT NULL,
        ra_deg         DOUBLE PRECISION NOT NULL,
        dec_deg        DOUBLE PRECISION NOT NULL,
        kind           TEXT NOT NULL,
        filter         TEXT DEFAULT 'CV',
        bjd            DOUBLE PRECISION,
        mag            DOUBLE PRECISION,
        delta_mag      DOUBLE PRECISION,
        node_id        TEXT DEFAULT '',
        contribution_id INTEGER,
        user_id        TEXT DEFAULT '',
        state          TEXT DEFAULT 'detected',
        vsx_name       TEXT DEFAULT '',
        tns_name       TEXT DEFAULT '',
        detail         TEXT DEFAULT '{}',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_retro_state ON retro_discoveries(state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_retro_coords ON retro_discoveries(ra_deg, dec_deg)",
    "CREATE INDEX IF NOT EXISTS idx_retro_user ON retro_discoveries(user_id)",
    # Asteroid/minor-planet astrometry: unmatched (uncatalogued) survey
    # detections, kept per-position (not quantized/deduped like survey_sources)
    # since a moving object's position changes every frame — this is the raw
    # material cloud.moving_objects links into tracklets.
    """
    CREATE TABLE IF NOT EXISTS moving_object_detections (
        id            BIGSERIAL PRIMARY KEY,
        node_id       TEXT NOT NULL,
        bjd           DOUBLE PRECISION NOT NULL,
        ra_deg        DOUBLE PRECISION NOT NULL,
        dec_deg       DOUBLE PRECISION NOT NULL,
        mag           DOUBLE PRECISION,
        mag_err       DOUBLE PRECISION,
        snr           DOUBLE PRECISION,
        filter        TEXT DEFAULT 'CV',
        frame_id      TEXT DEFAULT '',
        date_obs_utc  TEXT DEFAULT '',
        tracklet_id   INTEGER,
        link_done     BOOLEAN DEFAULT FALSE,
        created_at    TEXT NOT NULL,
        UNIQUE (node_id, bjd, ra_deg, dec_deg)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mod_unlinked ON moving_object_detections(node_id, bjd) "
    "WHERE tracklet_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_mod_tracklet ON moving_object_detections(tracklet_id)",
    """
    CREATE TABLE IF NOT EXISTS asteroid_candidates (
        id                   SERIAL PRIMARY KEY,
        node_id              TEXT NOT NULL,
        first_bjd            DOUBLE PRECISION NOT NULL,
        last_bjd             DOUBLE PRECISION NOT NULL,
        n_detections         INTEGER DEFAULT 0,
        ra0_deg              DOUBLE PRECISION NOT NULL,
        dec0_deg             DOUBLE PRECISION NOT NULL,
        ra_rate_deg_day      DOUBLE PRECISION,
        dec_rate_deg_day     DOUBLE PRECISION,
        fit_residual_arcsec  DOUBLE PRECISION,
        mean_mag             DOUBLE PRECISION,
        state                TEXT DEFAULT 'linked',
        skybot_name          TEXT DEFAULT '',
        designation          TEXT DEFAULT '',
        detail               TEXT DEFAULT '{}',
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_asteroid_cand_state ON asteroid_candidates(state, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS mpc_reports (
        id             SERIAL PRIMARY KEY,
        candidate_id   INTEGER REFERENCES asteroid_candidates(id),
        file_path      TEXT NOT NULL,
        format         TEXT DEFAULT 'ades_psv',
        n_observations INTEGER DEFAULT 0,
        created_at     TEXT NOT NULL
    )
    """,
    # Rotation light-curve follow-up: a schedule of same-night re-observation
    # slots for a confirmed, slow-moving (non-NEO) asteroid, dispatched as
    # ordinary `interrupts` when each slot comes due (cloud/moving_objects.py:
    # schedule_rotation_followup / dispatch_due_followups). Position at
    # dispatch time is re-extrapolated from the candidate's fit rate rather
    # than stored up front, since the slot may fire later than planned.
    """
    CREATE TABLE IF NOT EXISTS asteroid_followups (
        id            SERIAL PRIMARY KEY,
        candidate_id  INTEGER NOT NULL REFERENCES asteroid_candidates(id),
        node_id       TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        not_before    TEXT NOT NULL,
        fired_at      TEXT,
        interrupt_id  INTEGER,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_followups_due ON asteroid_followups(not_before) "
    "WHERE fired_at IS NULL",
    # ── Network photometric calibration mesh ────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS response_fingerprints (
        response_fingerprint  TEXT PRIMARY KEY,
        response_family       TEXT DEFAULT '',
        node_id               TEXT NOT NULL,
        descriptor            TEXT DEFAULT '{}',
        first_seen_at         TEXT NOT NULL,
        last_seen_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_response_family ON response_fingerprints(response_family, node_id)",
    """
    CREATE TABLE IF NOT EXISTS calibration_samples (
        id                    BIGSERIAL PRIMARY KEY,
        response_fingerprint  TEXT NOT NULL,
        response_family       TEXT DEFAULT '',
        node_id               TEXT NOT NULL,
        frame_id              TEXT NOT NULL,
        source_key            TEXT NOT NULL,
        bjd                   DOUBLE PRECISION NOT NULL,
        filter                TEXT NOT NULL DEFAULT 'CV',
        instrumental_mag      DOUBLE PRECISION NOT NULL,
        instrumental_err      DOUBLE PRECISION NOT NULL,
        frame_zero_point      DOUBLE PRECISION NOT NULL DEFAULT 0,
        catalog_mag           DOUBLE PRECISION NOT NULL,
        catalog_err           DOUBLE PRECISION NOT NULL,
        catalog_band          TEXT DEFAULT '',
        catalog_color         DOUBLE PRECISION,
        airmass               DOUBLE PRECISION,
        flags                 TEXT DEFAULT '[]',
        created_at            TEXT NOT NULL,
        UNIQUE(response_fingerprint, frame_id, source_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cal_samples_model ON calibration_samples(response_fingerprint, filter, bjd)",
    "CREATE INDEX IF NOT EXISTS idx_cal_samples_source ON calibration_samples(source_key, filter)",
    """
    CREATE TABLE IF NOT EXISTS photometric_models (
        model_version         TEXT PRIMARY KEY,
        response_fingerprint  TEXT NOT NULL,
        response_family       TEXT DEFAULT '',
        node_id               TEXT NOT NULL,
        filter                TEXT NOT NULL DEFAULT 'CV',
        state                 TEXT NOT NULL DEFAULT 'collecting',
        "offset"              DOUBLE PRECISION DEFAULT 0,
        color_term            DOUBLE PRECISION DEFAULT 0,
        extinction            DOUBLE PRECISION DEFAULT 0,
        drift_per_day         DOUBLE PRECISION DEFAULT 0,
        pivot_color           DOUBLE PRECISION DEFAULT 0,
        model_uncertainty     DOUBLE PRECISION DEFAULT 0,
        validation            TEXT DEFAULT '{}',
        consecutive_passes    INTEGER DEFAULT 0,
        created_at            TEXT NOT NULL,
        retired_at            TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_photomodel_active ON photometric_models(response_fingerprint, filter, state, created_at)",
    """
    CREATE TABLE IF NOT EXISTS calibration_opportunities (
        id                    BIGSERIAL PRIMARY KEY,
        node_id               TEXT NOT NULL,
        response_fingerprint  TEXT DEFAULT '',
        field_name            TEXT NOT NULL,
        plan_id               TEXT DEFAULT '',
        scheduled_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cal_opportunity_node ON calibration_opportunities(node_id, scheduled_at)",
    # ── GCN event normalization, tiling and centrally assigned tasks ────────
    """
    CREATE TABLE IF NOT EXISTS network_events (
        event_id          TEXT PRIMARY KEY,
        source_event_id   TEXT NOT NULL,
        source            TEXT NOT NULL,
        mission           TEXT DEFAULT '',
        topic             TEXT DEFAULT '',
        schema_version    TEXT DEFAULT '',
        event_class       TEXT DEFAULT 'unknown',
        role              TEXT DEFAULT 'observation',
        active_revision   INTEGER DEFAULT 0,
        status            TEXT DEFAULT 'received',
        event_time        TEXT DEFAULT '',
        received_time     TEXT NOT NULL,
        policy            TEXT DEFAULT '{}',
        cancellation_generation INTEGER DEFAULT 0,
        UNIQUE(source, source_event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_revisions (
        id                BIGSERIAL PRIMARY KEY,
        event_id          TEXT NOT NULL REFERENCES network_events(event_id),
        revision          INTEGER NOT NULL,
        notice_type       TEXT DEFAULT 'initial',
        significance      TEXT DEFAULT '{}',
        localization_type TEXT DEFAULT 'none',
        localization      TEXT DEFAULT '{}',
        area50_deg2       DOUBLE PRECISION,
        area90_deg2       DOUBLE PRECISION,
        distance          TEXT DEFAULT '{}',
        raw_notice        TEXT DEFAULT '{}',
        notice_hash       TEXT DEFAULT '',
        received_at       TEXT NOT NULL,
        UNIQUE(event_id, revision)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_revision_hash ON event_revisions(event_id, notice_hash) WHERE notice_hash<>''",
    """
    CREATE TABLE IF NOT EXISTS event_tiles (
        tile_id           TEXT PRIMARY KEY,
        event_id          TEXT NOT NULL REFERENCES network_events(event_id),
        event_revision    INTEGER NOT NULL,
        ra_deg            DOUBLE PRECISION NOT NULL,
        dec_deg           DOUBLE PRECISION NOT NULL,
        radius_deg        DOUBLE PRECISION NOT NULL,
        probability_mass  DOUBLE PRECISION DEFAULT 0,
        pass_number       INTEGER DEFAULT 1,
        status            TEXT DEFAULT 'candidate',
        detail            TEXT DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_event_tiles_event ON event_tiles(event_id, event_revision, status)",
    """
    CREATE TABLE IF NOT EXISTS observation_tasks (
        task_id           TEXT PRIMARY KEY,
        node_id           TEXT NOT NULL,
        event_id          TEXT DEFAULT '',
        event_revision    INTEGER DEFAULT 0,
        tile_id           TEXT DEFAULT '',
        ra_deg            DOUBLE PRECISION NOT NULL,
        dec_deg           DOUBLE PRECISION NOT NULL,
        earliest_utc      TEXT NOT NULL,
        latest_utc        TEXT NOT NULL,
        exposure          TEXT DEFAULT '{}',
        priority          DOUBLE PRECISION DEFAULT 0,
        state             TEXT DEFAULT 'pending',
        cancellation_generation INTEGER DEFAULT 0,
        result             TEXT DEFAULT '{}',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_node ON observation_tasks(node_id, state, latest_utc)",
    """
    CREATE TABLE IF NOT EXISTS galaxy_catalog (
        galaxy_id          TEXT PRIMARY KEY,
        ra_deg             DOUBLE PRECISION NOT NULL,
        dec_deg            DOUBLE PRECISION NOT NULL,
        distance_mpc       DOUBLE PRECISION,
        distance_err_mpc   DOUBLE PRECISION,
        luminosity_weight  DOUBLE PRECISION DEFAULT 1,
        catalog_name       TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_galaxy_radec ON galaxy_catalog(dec_deg, ra_deg)",
    "CREATE INDEX IF NOT EXISTS idx_galaxy_distance ON galaxy_catalog(distance_mpc)",
    # ── Signed autonomy bundles and idempotent execution reconciliation ─────
    """
    CREATE TABLE IF NOT EXISTS autonomy_bundles (
        bundle_id         TEXT PRIMARY KEY,
        node_id           TEXT NOT NULL,
        plan_id           TEXT NOT NULL,
        sequence          BIGINT NOT NULL,
        issued_at         TEXT NOT NULL,
        valid_from        TEXT NOT NULL,
        expires_at        TEXT NOT NULL,
        payload           TEXT NOT NULL,
        signature         TEXT DEFAULT '',
        signing_key_id    TEXT DEFAULT '',
        status            TEXT DEFAULT 'current',
        reconciliation    TEXT DEFAULT '{}',
        UNIQUE(node_id, sequence)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bundle_node ON autonomy_bundles(node_id, status, sequence)",
    """
    CREATE TABLE IF NOT EXISTS execution_outcomes (
        attempt_id        TEXT PRIMARY KEY,
        node_id           TEXT NOT NULL,
        item_id           TEXT DEFAULT '',
        bundle_id         TEXT DEFAULT '',
        task_id           TEXT DEFAULT '',
        state             TEXT NOT NULL,
        started_at        TEXT DEFAULT '',
        finished_at       TEXT DEFAULT '',
        frames_attempted  INTEGER DEFAULT 0,
        frames_completed  INTEGER DEFAULT 0,
        last_checkpoint   TEXT DEFAULT '',
        failure_reason    TEXT DEFAULT '',
        detail            TEXT DEFAULT '{}',
        received_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outcomes_bundle ON execution_outcomes(bundle_id, item_id)",
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
    global _DB_URL, _pool
    with _init_lock:
        _DB_URL = url or os.environ.get("DATABASE_URL", "")
        if not _DB_URL:
            raise RuntimeError(
                "No database URL configured. Set DATABASE_URL or pass url to db.init()."
            )
        _pool = ThreadedConnectionPool(minconn=2, maxconn=20, dsn=_DB_URL)
        conn = _pool.getconn()
        try:
            cur = conn.cursor()
            for stmt in _SCHEMA:
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            for stmt in _LATE_TABLES:
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            # Migrations run after both base and late tables exist so column
            # additions can target either group (e.g. contributions columns).
            _run_migrations(conn)
            for stmt in _SEEDS:
                cur.execute(stmt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _pool.putconn(conn)
        logger.info("Database ready: %s", _DB_URL.split("@")[-1])


def connect():
    """Get a connection from the pool. Call db.release(conn) when done."""
    if _pool is None:
        raise RuntimeError("cloud.db.init() has not been called")
    return _pool.getconn()


def release(conn) -> None:
    """Return a pooled connection. Called in finally blocks instead of close()."""
    if _pool is not None:
        _pool.putconn(conn)


# ── Convenience helpers ────────────────────────────────────────────────────────

def query(sql: str, params: tuple = ()) -> list:
    """Run a SELECT and return a list of plain dicts."""
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        conn.rollback()
        raise
    finally:
        release(conn)


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
        release(conn)


def executemany(sql: str, seq: list) -> None:
    conn = connect()
    try:
        with conn:
            cur = conn.cursor()
            cur.executemany(sql, seq)
    finally:
        release(conn)


def loads(text: Any, default: Any = None) -> Any:
    """Tolerant JSON column decoder."""
    if not text:
        return default if default is not None else {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default if default is not None else {}
