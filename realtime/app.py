#!/usr/bin/env python3
"""
THE ORGANISM — realtime push gateway (SSE).

A dedicated service so long-lived connections never pin threads on the main
API's small gunicorn pool. Direction is strictly server→node/member, so
Server-Sent Events beat WebSocket: reconnection + replay (Last-Event-ID) is
built in, and it rides Railway's HTTP edge with no upgrade handshake.

Wiring:
    main API  --live.publish()-->  dispatch_events row + pg_notify('dispatch')
    this svc  --LISTEN 'dispatch'-->  fan out to per-connection queues  --SSE-->  node

The stream carries *signals* only ("plan", "interrupt", "retask"). A node that
receives one re-fetches content over the authenticated main API, so the stream
is never trusted for payloads and the node's polling remains a full fallback.

Run (production):  gunicorn -k gevent -w 1 --bind 0.0.0.0:$PORT realtime.wsgi:app
Run (dev):         python -m realtime.app  cloud/config.yaml
"""

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extensions
from flask import Flask, Response, jsonify, request, stream_with_context

from cloud import auth, db, live, registry

logger = logging.getLogger("realtime")

app = Flask(__name__)

# node_id → set of live subscriber queues. Guarded by _subs_lock. Members
# subscribing to the whole fleet register under the sentinel key "*fleet*".
_FLEET_KEY = "*fleet*"
_subs: dict[str, set] = {}
_subs_lock = threading.Lock()

# Heartbeat comment cadence: keeps proxies from idling the connection out and
# lets the client notice a dead link. Well under typical 60 s proxy timeouts.
_KEEPALIVE_S = 20.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _subscribe(key: str) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=256)
    with _subs_lock:
        _subs.setdefault(key, set()).add(q)
    return q


def _unsubscribe(key: str, q: queue.Queue) -> None:
    with _subs_lock:
        conns = _subs.get(key)
        if conns:
            conns.discard(q)
            if not conns:
                _subs.pop(key, None)


def _fan_out(node_id: str, event: dict) -> None:
    """Deliver an event to every queue watching this node, plus fleet watchers.

    A full queue means a stuck/slow client — drop the event for that client
    rather than block the listener; the client replays on reconnect.
    """
    with _subs_lock:
        targets = list(_subs.get(node_id, ())) + list(_subs.get(_FLEET_KEY, ()))
    for q in targets:
        try:
            q.put_nowait(event)
        except queue.Full:
            logger.debug("dropping event for slow subscriber on %s", node_id)


# ── Postgres LISTEN loop ────────────────────────────────────────────────────────

def _listen_loop(dsn: str) -> None:
    """Hold one dedicated connection LISTENing on the dispatch channel and fan
    each NOTIFY out to subscribers. Reconnects forever on failure."""
    while True:
        conn = None
        try:
            conn = psycopg2.connect(dsn)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute(f"LISTEN {live.NOTIFY_CHANNEL}")
            logger.info("Listening on Postgres channel '%s'", live.NOTIFY_CHANNEL)
            while True:
                # gevent (via psycogreen) makes this select cooperative; under
                # the dev server it's a plain blocking wait in this thread.
                if not _wait_readable(conn, timeout=30.0):
                    continue
                conn.poll()
                while conn.notifies:
                    note = conn.notifies.pop(0)
                    _handle_notify(note.payload)
        except Exception as exc:
            logger.warning("LISTEN loop error: %s — reconnecting in 3s", exc)
            time.sleep(3)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _wait_readable(conn, timeout: float) -> bool:
    import select
    try:
        r, _, _ = select.select([conn], [], [], timeout)
        return bool(r)
    except Exception:
        return False


def _handle_notify(payload: str) -> None:
    try:
        note = json.loads(payload)
    except (ValueError, TypeError):
        return
    node_id = note.get("node_id")
    if not node_id:
        return
    _fan_out(node_id, {
        "id":   note.get("id"),
        "kind": note.get("kind", "event"),
        "node_id": node_id,
    })


# ── SSE endpoints ───────────────────────────────────────────────────────────────

def _sse(event: dict) -> str:
    eid = event.get("id")
    prefix = f"id: {eid}\n" if eid is not None else ""
    return f"{prefix}event: {event.get('kind', 'event')}\ndata: {json.dumps(event)}\n\n"


def _stream(key: str, replay_node: str | None, last_id: int):
    """Generator: replay missed events, then live-stream with keepalives."""
    q = _subscribe(key)
    try:
        yield "retry: 3000\n\n"
        # Replay anything published while the client was disconnected so a
        # dropped SSE link never loses a dispatch signal.
        if replay_node and last_id:
            try:
                for ev in live.events_since(replay_node, last_id):
                    yield _sse({"id": ev["id"], "kind": ev["kind"],
                                "node_id": replay_node})
            except Exception as exc:
                logger.debug("replay failed: %s", exc)
        while True:
            try:
                ev = q.get(timeout=_KEEPALIVE_S)
                yield _sse(ev)
            except queue.Empty:
                yield ": keepalive\n\n"
    finally:
        _unsubscribe(key, q)


def _last_event_id() -> int:
    raw = request.headers.get("Last-Event-ID") or request.args.get("last_id") or "0"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


@app.route("/api/v1/stream", methods=["GET"])
def stream_node():
    """Per-node stream. Auth by node_id + api_key (query params — EventSource
    can't set headers)."""
    node = registry.authenticate(
        request.args.get("node_id", ""), request.args.get("api_key", ""))
    if node is None:
        return jsonify({"error": "invalid node credentials"}), 401
    node_id = node["node_id"]
    resp = Response(
        stream_with_context(_stream(node_id, node_id, _last_event_id())),
        mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/v1/stream/fleet", methods=["GET"])
def stream_fleet():
    """Whole-fleet stream for the member 'organism' view. Member token auth."""
    token = (request.args.get("token")
             or (request.headers.get("Authorization", "")[7:]
                 if request.headers.get("Authorization", "").startswith("Bearer ")
                 else ""))
    if auth.verify_token(token) is None:
        return jsonify({"error": "authentication required"}), 401
    resp = Response(
        stream_with_context(_stream(_FLEET_KEY, None, 0)),
        mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/v1/health", methods=["GET"])
def health():
    with _subs_lock:
        n = sum(len(v) for v in _subs.values())
    return jsonify({"ok": True, "subscribers": n, "server_time": _now()})


def create_app(config: dict):
    """Init DB and start the LISTEN thread. Idempotent."""
    dsn = config.get("database", {}).get("url", "") or os.environ.get("DATABASE_URL", "")
    db.init(dsn)
    resolved = os.environ.get("DATABASE_URL", "") or dsn
    t = threading.Thread(target=_listen_loop, args=(resolved,),
                         daemon=True, name="pg-listen")
    t.start()
    return app


def main() -> None:
    import sys
    from cloud.main import load_config
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    logging.basicConfig(
        level=config.get("logging", {}).get("level", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    create_app(config)
    port = int(os.environ.get("PORT", config.get("realtime", {}).get("port", 8810)))
    logger.info("Realtime SSE gateway on :%d", port)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
