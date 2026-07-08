"""A programmable fake Telescope Net cloud for fault-injection tests.

Runs a real HTTP server on an ephemeral port so the CloudCommunicator is
exercised through its actual network stack.  Behavior is switched per-test:

    fake.mode = "ok"        → normal responses
    fake.mode = "http500"   → every request returns 500
    fake.mode = "reject"    → registration/uploads rejected with 400/409
    fake.mode = "down"      → connections are dropped without a response
"""

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeCloud:
    def __init__(self):
        self.mode = "ok"
        self.requests: list[dict] = []   # {"method", "path", "body"}
        self._lock = threading.Lock()
        # THE ORGANISM: programmable realtime push. Tests call push("interrupt")
        # to fire an SSE event to any node connected on /api/v1/stream, and set
        # `interrupts` to what /api/v1/interrupts returns.
        self._sse_events: "queue.Queue[str]" = queue.Queue()
        self.interrupts: list[dict] = []
        # Status the fake returns for member contribution uploads (200 ok,
        # 409 duplicate, 429 cap) — set per-test.
        self.contrib_status = 200

        fake = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.1 so the SSE stream can deliver chunks incrementally
            # instead of the client buffering a 1.0 body until connection close.
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                    import gzip
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                with fake._lock:
                    fake.requests.append({
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                    })
                return body

            def _reply(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self):
                """Serve the SSE gateway: block until push() fires an event.

                Uses chunked transfer-encoding so each frame reaches the client
                immediately (the real Flask gateway streams the same way)."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def _chunk(data: bytes):
                    self.wfile.write(f"{len(data):X}\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

                try:
                    _chunk(b"retry: 1000\n\n")
                    while True:
                        try:
                            kind = fake._sse_events.get(timeout=1.0)
                        except queue.Empty:
                            _chunk(b": keepalive\n\n")
                            continue
                        _chunk(f"event: {kind}\ndata: {{}}\n\n".encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def _handle(self):
                self._record()
                if fake.mode == "down":
                    self.connection.close()
                    return
                if fake.mode == "http500":
                    self._reply(500, {"error": "internal error"})
                    return
                if fake.mode == "reject":
                    self._reply(409, {"error": "activation code already used"})
                    return
                # mode == "ok"
                if self.path.startswith("/api/v1/stream"):
                    self._stream()
                elif self.path == "/api/v1/nodes/register":
                    self._reply(200, {"node_id": "node_test01",
                                      "api_key": "key_test01"})
                elif self.path == "/api/v1/nodes/heartbeat":
                    self._reply(200, {"ok": True})
                elif self.path == "/api/v1/measurements":
                    self._reply(200, {"ok": True, "id": 1})
                elif self.path == "/api/v1/survey":
                    self._reply(200, {"ok": True, "accepted": 0})
                elif self.path == "/api/v1/nodes/characterization":
                    self._reply(200, {"ok": True, "applied": {}})
                elif self.path == "/api/v1/incidents":
                    self._reply(200, {"ok": True})
                elif self.path == "/api/v1/me/contributions":
                    st = fake.contrib_status
                    self._reply(st, {"ok": st == 200} if st != 200
                                else {"ok": True, "contribution_id": 1,
                                      "status": "pending"})
                elif self.path.startswith("/api/v1/interrupts"):
                    self._reply(200, {"interrupts": list(fake.interrupts)})
                elif self.path.startswith("/api/v1/plan"):
                    self._reply(200, {"plan": None})
                else:
                    self._reply(200, {"ok": True})

            do_GET = _handle
            do_POST = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="fake-cloud")

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def paths(self, suffix: str = "") -> list[str]:
        with self._lock:
            return [r["path"] for r in self.requests
                    if r["path"].endswith(suffix) or not suffix]

    def push(self, kind: str = "interrupt"):
        """Fire an SSE event of `kind` to a connected node stream."""
        self._sse_events.put(kind)

    def last_state(self) -> dict:
        """The `state` block from the most recent heartbeat POST, or {}."""
        with self._lock:
            for r in reversed(self.requests):
                if r["path"].endswith("/heartbeat"):
                    return r["body"].get("state") or {}
        return {}

    def count_paths(self, needle: str) -> int:
        with self._lock:
            return sum(1 for r in self.requests if needle in r["path"])

    def clear(self):
        with self._lock:
            self.requests.clear()
