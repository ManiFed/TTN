"""Long-lived NASA GCN Kafka consumer with offset-based reconnect recovery."""

from __future__ import annotations

import io
import json
import logging
import struct
import threading
import time
from xml.etree import ElementTree

from cloud import gcn_events

logger = logging.getLogger("cloud.gcn")


def decode_notice(value, *, schema_registry: str = "") -> dict:
    if isinstance(value, dict):
        return value
    raw = bytes(value)
    stripped = raw.lstrip()
    if stripped.startswith((b"{", b"[")):
        decoded = json.loads(stripped.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {"records": decoded}
    if stripped.startswith(b"<"):
        root = ElementTree.fromstring(stripped)
        out = {"ivorn": root.attrib.get("ivorn", ""), "role": root.attrib.get("role", "")}
        for param in root.findall(".//{*}Param"):
            if param.attrib.get("name"):
                out[param.attrib["name"]] = param.attrib.get("value")
        return out
    # Confluent Avro framing: magic byte + 4-byte schema id + datum.
    if len(raw) > 5 and raw[0] == 0 and schema_registry:
        import requests
        from fastavro import schemaless_reader
        schema_id = struct.unpack(">I", raw[1:5])[0]
        response = requests.get(
            schema_registry.rstrip("/") + f"/schemas/ids/{schema_id}", timeout=10)
        response.raise_for_status()
        schema = json.loads(response.json()["schema"])
        return schemaless_reader(io.BytesIO(raw[5:]), schema)
    # GCN Classic-over-Kafka text notices use one KEY: value field per line.
    # Keep their original names and add common canonical aliases so the same
    # normalization path can ingest both legacy and schema-based notices.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("unsupported GCN notice encoding") from exc
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    if not out:
        raise ValueError("unsupported GCN notice encoding")
    aliases = {
        "TRIGGER_NUM": "trigger_id", "TRIGGER_ID": "trigger_id",
        "NOTICE_TYPE": "notice_type", "NOTICE_DATE": "time_created",
        "EVENT_TIME": "event_time", "GRB_RA": "ra", "GRB_DEC": "dec",
        "SRC_RA": "ra", "SRC_DEC": "dec", "ERROR": "error_radius",
    }
    for old, new in aliases.items():
        if old in out and new not in out:
            # Classic coordinates often append units and confidence text.
            token = out[old].split()[0].rstrip("d,")
            try:
                out[new] = float(token) if new in ("ra", "dec", "error_radius") else token
            except ValueError:
                out[new] = token
    return out


class GCNConsumerService:
    def __init__(self, config: dict):
        self.config = config
        self.gcfg = config.get("gcn") or {}
        self._stop = threading.Event()
        self._thread = None
        self.last_heartbeat = 0.0

    def start(self) -> None:
        if not self.gcfg.get("enabled", False) or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="gcn-kafka")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._consume()
                backoff = 2.0
            except Exception as exc:
                logger.error("GCN consumer disconnected: %s", exc)
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)

    def _consume(self) -> None:
        from gcn_kafka import Consumer
        consumer = Consumer(
            client_id=str(self.gcfg.get("client_id") or ""),
            client_secret=str(self.gcfg.get("client_secret") or ""),
            domain=str(self.gcfg.get("domain") or "gcn.nasa.gov"))
        topics = list(self.gcfg.get("topics") or [])
        if "gcn.heartbeat" not in topics:
            topics.append("gcn.heartbeat")
        consumer.subscribe(topics)
        logger.info("GCN consumer subscribed to %d topics", len(topics))
        connected_at = time.time()
        stale_reported = False
        try:
            while not self._stop.is_set():
                for message in consumer.consume(timeout=1):
                    if self._stop.is_set():
                        break
                    topic = message.topic()
                    if topic == "gcn.heartbeat":
                        self.last_heartbeat = time.time()
                        stale_reported = False
                        continue
                    try:
                        body = decode_notice(
                            message.value(), schema_registry=str(
                                self.gcfg.get("schema_registry") or ""))
                        result = gcn_events.ingest(topic, body, self.config)
                        logger.info("GCN %s -> %s revision %s (%s)", topic,
                                    result["event_id"], result["revision"],
                                    result.get("policy", {}).get("reason", "duplicate"))
                    except Exception as exc:
                        logger.exception("Malformed GCN notice on %s: %s", topic, exc)
                heartbeat_age = time.time() - (self.last_heartbeat or connected_at)
                stale_after = float(self.gcfg.get("heartbeat_stale_s", 120))
                if heartbeat_age > stale_after and not stale_reported:
                    logger.warning("GCN heartbeat stale for %.0f seconds", heartbeat_age)
                    stale_reported = True
        finally:
            close = getattr(consumer, "close", None)
            if close:
                close()
