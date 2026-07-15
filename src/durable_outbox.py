"""Transactional, byte-bounded node outbox for disconnected operation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


class DurableOutbox:
    def __init__(self, path: Path, max_bytes: int = 2 * 1024**3):
        self.path = Path(path)
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self._lock = threading.RLock()
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
                "CREATE TABLE IF NOT EXISTS outbox ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,"
                " idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,"
                " size_bytes INTEGER NOT NULL, priority INTEGER NOT NULL DEFAULT 50,"
                " created_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_send "
                         "ON outbox(priority DESC, id ASC)")

    def enqueue(self, topic: str, payload: dict, *, priority: int = 50,
                idempotency_key: str = "") -> bool:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        key = idempotency_key or hashlib.sha256(
            f"{topic}:{body}".encode()).hexdigest()
        with self._lock, self._connect() as conn:
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO outbox(topic,idempotency_key,payload,size_bytes,priority,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (topic, key, body, len(body.encode()), int(priority), time.time()))
            inserted = conn.total_changes > before
            self._prune(conn)
            return inserted

    def _prune(self, conn):
        total = int(conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM outbox").fetchone()[0])
        # Drop the oldest lowest-priority records first. Measurements and
        # outcomes use priority >= 90 and therefore survive telemetry pressure.
        while total > self.max_bytes:
            row = conn.execute(
                "SELECT id,size_bytes FROM outbox WHERE priority < 90 "
                "ORDER BY priority ASC,id ASC LIMIT 1").fetchone()
            if row is None:
                break
            conn.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            total -= int(row["size_bytes"])

    def usage_bytes(self) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM outbox").fetchone()[0])

    def over_capacity(self) -> bool:
        """True when retained science has consumed the configured byte budget."""
        return self.usage_bytes() >= self.max_bytes

    def peek(self, topic: str | None = None, limit: int = 25) -> list[dict]:
        with self._lock, self._connect() as conn:
            if topic:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE topic=? ORDER BY priority DESC,id ASC LIMIT ?",
                    (topic, int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outbox ORDER BY priority DESC,id ASC LIMIT ?",
                    (int(limit),)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def ack(self, row_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM outbox WHERE id=?", (int(row_id),))

    def failed(self, row_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (int(row_id),))

    def count(self, topic: str | None = None) -> int:
        with self._lock, self._connect() as conn:
            if topic:
                return int(conn.execute("SELECT COUNT(*) FROM outbox WHERE topic=?", (topic,)).fetchone()[0])
            return int(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def clear(self, topic: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            if topic:
                conn.execute("DELETE FROM outbox WHERE topic=?", (topic,))
            else:
                conn.execute("DELETE FROM outbox")

    def migrate_json(self, path: Path, topic: str, priority: int = 50) -> int:
        path = Path(path)
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        count = 0
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and self.enqueue(topic, row, priority=priority):
                    count += 1
        if rows:
            migrated = path.with_suffix(path.suffix + ".migrated")
            try:
                path.replace(migrated)
            except OSError:
                pass
        return count
