"""A programmable fake Telescope Net cloud for fault-injection tests.

Runs a real HTTP server on an ephemeral port so the CloudCommunicator is
exercised through its actual network stack.  Behavior is switched per-test:

    fake.mode = "ok"        → normal responses
    fake.mode = "http500"   → every request returns 500
    fake.mode = "reject"    → registration/uploads rejected with 400/409
    fake.mode = "down"      → connections are dropped without a response
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeCloud:
    def __init__(self):
        self.mode = "ok"
        self.requests: list[dict] = []   # {"method", "path", "body"}
        self._lock = threading.Lock()

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
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
                if self.path == "/api/v1/nodes/register":
                    self._reply(200, {"node_id": "node_test01",
                                      "api_key": "key_test01"})
                elif self.path == "/api/v1/nodes/heartbeat":
                    self._reply(200, {"ok": True})
                elif self.path == "/api/v1/measurements":
                    self._reply(200, {"ok": True, "id": 1})
                elif self.path == "/api/v1/incidents":
                    self._reply(200, {"ok": True})
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

    def clear(self):
        with self._lock:
            self.requests.clear()
