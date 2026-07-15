"""Node-side verification, persistence and execution journal for autonomy."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def canonical_payload(bundle: dict) -> bytes:
    body = dict(bundle)
    body.pop("signature", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AutonomyStore:
    def __init__(self, path: Path = Path("data") / "autonomy.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS bundles ("
                " bundle_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, node_id TEXT NOT NULL,"
                " expires_at TEXT NOT NULL, payload TEXT NOT NULL, current INTEGER DEFAULT 1)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS journal ("
                " attempt_id TEXT PRIMARY KEY, bundle_id TEXT NOT NULL, item_id TEXT NOT NULL,"
                " task_id TEXT DEFAULT '', state TEXT NOT NULL, started_at TEXT DEFAULT '',"
                " finished_at TEXT DEFAULT '', frames_attempted INTEGER DEFAULT 0,"
                " frames_completed INTEGER DEFAULT 0, last_checkpoint TEXT DEFAULT '',"
                " failure_reason TEXT DEFAULT '',"
                " detail TEXT DEFAULT '{}', uploaded INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")

    def _ensure_journal(self):
        with self._connect() as conn:
            conn.execute("DROP TABLE IF EXISTS journal_bad")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS journal ("
                " attempt_id TEXT PRIMARY KEY, bundle_id TEXT NOT NULL, item_id TEXT NOT NULL,"
                " task_id TEXT DEFAULT '', state TEXT NOT NULL, started_at TEXT DEFAULT '',"
                " finished_at TEXT DEFAULT '', frames_attempted INTEGER DEFAULT 0,"
                " frames_completed INTEGER DEFAULT 0, last_checkpoint TEXT DEFAULT '',"
                " failure_reason TEXT DEFAULT '',"
                " detail TEXT DEFAULT '{}', uploaded INTEGER DEFAULT 0)")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(journal)").fetchall()}
            if "last_checkpoint" not in columns:
                conn.execute("ALTER TABLE journal ADD COLUMN last_checkpoint TEXT DEFAULT ''")

    def _trusted_keys(self) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='trusted_keys'").fetchone()
        try:
            return json.loads(row["value"]) if row else {}
        except Exception:
            return {}

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        parts = []
        for token in str(value or "0").split("."):
            try:
                parts.append(int(token.split("-")[0]))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    def verify_and_store(self, bundle: dict, node_id: str,
                         public_keys: dict[str, str], *, agent_version: str = "999",
                         commissioning_complete: bool = True,
                         location_valid: bool = True,
                         max_storage_bytes: int = 20 * 1024**3) -> dict:
        """Verify authority and bounds, then atomically make a bundle current."""
        if int(bundle.get("schema_version") or 0) != 1:
            raise ValueError("unsupported autonomy bundle schema")
        if str(bundle.get("node_id") or "") != node_id:
            raise ValueError("autonomy bundle belongs to a different node")
        key_id = str(bundle.get("signing_key_id") or "")
        trusted = {**self._trusted_keys(), **public_keys}
        encoded = str(trusted.get(key_id) or "")
        if not encoded:
            raise ValueError("untrusted autonomy signing key")
        try:
            signature = base64.b64decode(bundle.get("signature") or "")
            key_bytes = base64.b64decode(encoded)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                signature, canonical_payload(bundle))
        except Exception as exc:
            raise ValueError("invalid autonomy bundle signature") from exc
        now = datetime.now(timezone.utc)
        valid_from, expires = _parse(bundle["valid_from"]), _parse(bundle["expires_at"])
        if expires <= now or expires <= valid_from:
            raise ValueError("expired autonomy bundle")
        if (expires - valid_from).total_seconds() > 18 * 3600 + 1:
            raise ValueError("autonomy bundle exceeds maximum lifetime")
        if self._version_tuple(agent_version) < self._version_tuple(
                str(bundle.get("minimum_agent_version") or "0")):
            raise ValueError("autonomy bundle requires a newer node agent")
        if (bundle.get("requirements") or {}).get("commissioning_complete") \
                and not commissioning_complete:
            raise ValueError("node commissioning is incomplete")
        if not location_valid:
            raise ValueError("node location or timezone is invalid")
        budgets = bundle.get("budgets") or {}
        if (int(budgets.get("max_items") or 0) > 100
                or int(budgets.get("max_slews") or 0) > 100
                or float(budgets.get("max_exposure_s") or 0) > 18 * 3600
                or int(budgets.get("max_storage_bytes") or 0) > int(max_storage_bytes)):
            raise ValueError("autonomy bundle exceeds local resource bounds")
        total_items = len(bundle.get("items") or []) + len(
            (bundle.get("contingencies") or {}).get("alternates") or [])
        if total_items > int(budgets.get("max_items") or 0):
            raise ValueError("autonomy bundle item count exceeds budget")
        all_items = list(bundle.get("items") or []) + list(
            (bundle.get("contingencies") or {}).get("alternates") or [])
        exposure_s = sum(float(i.get("expDur") or 0) * int(i.get("expCount") or 0)
                         for i in all_items)
        if exposure_s > float(budgets.get("max_exposure_s") or 0):
            raise ValueError("autonomy bundle exposure total exceeds budget")
        if total_items > int(budgets.get("max_slews") or 0):
            raise ValueError("autonomy bundle slew count exceeds budget")
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(sequence) AS n FROM bundles").fetchone()
            if row and int(row["n"] or 0) > int(bundle["sequence"]):
                raise ValueError("autonomy bundle sequence rollback")
            if row and int(row["n"] or 0) == int(bundle["sequence"]):
                prior = conn.execute(
                    "SELECT bundle_id,payload FROM bundles WHERE sequence=? LIMIT 1",
                    (int(bundle["sequence"]),)).fetchone()
                if (not prior or str(prior["bundle_id"]) != str(bundle["bundle_id"])
                        or json.loads(prior["payload"]) != bundle):
                    raise ValueError("autonomy bundle sequence was reused")
                return bundle
            conn.execute("UPDATE bundles SET current=0")
            conn.execute(
                "INSERT OR REPLACE INTO bundles(bundle_id,sequence,node_id,expires_at,payload,current) "
                "VALUES(?,?,?,?,?,1)",
                (bundle["bundle_id"], int(bundle["sequence"]), node_id,
                 bundle["expires_at"], json.dumps(bundle, sort_keys=True)))
            rotation = bundle.get("next_public_key") or {}
            if rotation.get("key_id") and rotation.get("public_key"):
                trusted[str(rotation["key_id"])] = str(rotation["public_key"])
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES('trusted_keys',?)",
                    (json.dumps(trusted, sort_keys=True),))
        self._ensure_journal()
        return bundle

    def active(self, node_id: str, *, clock_qualified: bool = True,
               now: datetime | None = None) -> dict | None:
        if not clock_qualified:
            return None
        now = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM bundles WHERE current=1 AND node_id=? "
                "ORDER BY sequence DESC LIMIT 1", (node_id,)).fetchone()
        if not row:
            return None
        bundle = json.loads(row["payload"])
        if not (_parse(bundle["valid_from"]) <= now < _parse(bundle["expires_at"])):
            return None
        return bundle

    def completed_items(self, bundle_id: str) -> set[str]:
        self._ensure_journal()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT item_id FROM journal WHERE bundle_id=? "
                "AND state IN ('completed','skipped','failed','cancelled')", (bundle_id,)).fetchall()
        return {str(r["item_id"]) for r in rows}

    def remaining_items(self, bundle: dict) -> list:
        done = self.completed_items(str(bundle.get("bundle_id") or ""))
        return [dict(i) for i in (bundle.get("items") or [])
                if str(i.get("item_id") or "") not in done]

    def contingency_items(self, bundle: dict) -> list:
        bundle_id = str(bundle.get("bundle_id") or "")
        with self._connect() as conn:
            failed = conn.execute(
                "SELECT 1 FROM journal WHERE bundle_id=? AND state IN ('failed','skipped') LIMIT 1",
                (bundle_id,)).fetchone()
        if not failed:
            return []
        done = self.completed_items(bundle_id)
        for item in (bundle.get("contingencies") or {}).get("alternates") or []:
            if str(item.get("item_id") or "") not in done:
                return [dict(item)]
        return []

    def record(self, bundle_id: str, item_id: str, state: str, *,
               attempt_id: str = "", task_id: str = "", started_at: str = "",
               finished_at: str = "", frames_attempted: int = 0,
               frames_completed: int = 0, last_checkpoint: str = "",
               failure_reason: str = "",
               detail: dict | None = None) -> str:
        self._ensure_journal()
        attempt_id = attempt_id or "attempt_" + uuid.uuid4().hex
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT started_at FROM journal WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not started_at and previous:
                started_at = str(previous["started_at"] or "")
            conn.execute(
                "INSERT OR REPLACE INTO journal "
                "(attempt_id,bundle_id,item_id,task_id,state,started_at,finished_at,"
                " frames_attempted,frames_completed,last_checkpoint,failure_reason,detail,uploaded) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (attempt_id, bundle_id, item_id, task_id, state, started_at,
                 finished_at, int(frames_attempted), int(frames_completed),
                 last_checkpoint, failure_reason[:500], json.dumps(detail or {})))
        return attempt_id

    def pending_outcomes(self, limit: int = 1000) -> list[dict]:
        self._ensure_journal()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal WHERE uploaded=0 ORDER BY rowid LIMIT ?",
                (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.pop("uploaded", None)
            item["detail"] = json.loads(item.get("detail") or "{}")
            out.append(item)
        return out

    def mark_uploaded(self, attempt_ids: list[str]) -> None:
        if not attempt_ids:
            return
        self._ensure_journal()
        with self._connect() as conn:
            conn.executemany("UPDATE journal SET uploaded=1 WHERE attempt_id=?",
                             [(v,) for v in attempt_ids])

    def set_clock_qualified(self, qualified: bool, skew_s: float = 0.0) -> None:
        payload = {"qualified": bool(qualified), "skew_s": float(skew_s),
                   "at": datetime.now(timezone.utc).isoformat()}
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('clock',?)",
                         (json.dumps(payload),))

    def recent_clock_qualified(self, max_age_hours: float = 12.0) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='clock'").fetchone()
        if not row:
            return False
        try:
            value = json.loads(row["value"])
            age = datetime.now(timezone.utc) - _parse(value["at"])
            return bool(value.get("qualified")) and age.total_seconds() <= max_age_hours * 3600
        except Exception:
            return False
