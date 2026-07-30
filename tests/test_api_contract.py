"""Client/server API contract drift.

The worst outage in this project's history was not a crash — it was drift.
The iOS app kept calling `/api/v1/me/activation-code` and posting
`{pairing_token, activation_code}` long after the cloud retired activation
codes, so every attempt to link a telescope failed with a 410/400 and no
test noticed. Members simply could not onboard.

Nothing here runs the cloud. It reads the paths each client actually calls
out of the source, and checks them against the routes the cloud actually
serves:

  1. every path a client calls must be routable (no vanished endpoints)
  2. no client may call an endpoint the cloud has retired (410 Gone)

Clients checked: the node agent (src/) and the desktop member app
(app/lib/). Servers: cloud/server.py and the realtime SSE service.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# A path segment standing in for a client-side interpolation, used only to
# make a template concrete enough for the router to match.
_PLACEHOLDER = "1"

# f-string / template interpolations in call sites:
#   {self._pair_token}  ${nodeId}  $id  {patch_id}
_INTERP = re.compile(r"\{[^}]*\}|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")


def _concrete(path: str) -> str:
    """Replace client-side interpolation with a matchable segment."""
    return _INTERP.sub(_PLACEHOLDER, path)


def _python_client_paths() -> dict[str, set[str]]:
    """/api/... paths referenced anywhere in the node agent source."""
    found: dict[str, set[str]] = {}
    for py in sorted((REPO / "src").rglob("*.py")):
        text = py.read_text(errors="replace")
        for raw in re.findall(r"/api/v\d+/[A-Za-z0-9_/{}$.\[\]'-]*", text):
            path = _concrete(raw).rstrip("/.")
            # Trailing artefacts from f-string slicing, e.g. a stray quote.
            path = path.split("'")[0].split('"')[0]
            if path.count("/") >= 3:
                found.setdefault(path, set()).add(py.name)
    return found


def _dart_client_paths() -> dict[str, set[str]]:
    """Paths passed to the app's _get/_post/_put/_patch/_delete helpers.

    AppConfig.apiPrefix ('/api/v1') is prepended by AppConfig.uri, so the
    call sites carry only the suffix.
    """
    found: dict[str, set[str]] = {}
    call = re.compile(r"_(get|post|put|patch|delete)\(\s*'([^']+)'")
    for dart in sorted((REPO / "app" / "lib").rglob("*.dart")):
        text = dart.read_text(errors="replace")
        for _method, raw in call.findall(text):
            if not raw.startswith("/"):
                continue
            found.setdefault("/api/v1" + _concrete(raw), set()).add(dart.name)
    return found


def _served_rules() -> list:
    """Every rule the cloud API and the realtime service expose."""
    import cloud.server as server
    rules = list(server.app.url_map.iter_rules())
    try:
        import realtime.app as realtime
        rules += list(realtime.app.url_map.iter_rules())
    except Exception:
        pass          # realtime service is optional for this check
    return rules


def _retired_paths() -> set[str]:
    """Routes whose handler unconditionally returns 410 Gone.

    Detected structurally: a route decorator followed, before the next
    route, by a `), 410` return with no branching in between.
    """
    text = (REPO / "cloud" / "server.py").read_text()
    retired: set[str] = set()
    blocks = text.split("@app.route(")
    for block in blocks[1:]:
        match = re.match(r'\s*["\']([^"\']+)["\']', block)
        if not match:
            continue
        body = block.split("\n@app.route(")[0]
        # Body up to the next decorated function only.
        body = body[: body.find("\n@app.route") if "\n@app.route" in body else len(body)]
        if re.search(r",\s*410\b", body) and " if " not in body.split(", 410")[0][-400:]:
            retired.add(match.group(1))
    return retired


class ApiContractTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        from werkzeug.routing import Map
        # Match against a map built from the real routes minus the website
        # catch-all. Matching against the live map is useless here: the
        # `/<path:filename>` marketing-site route matches every URL and every
        # method, so a deleted endpoint would still look routable and this
        # whole test would pass vacuously.
        cls.rules = [r for r in _served_rules() if "<path:" not in r.rule]
        cls.adapter_maps = [
            Map([r.empty() for r in cls.rules]).bind("contract.test")]

    def _routable(self, path: str) -> bool:
        from werkzeug.exceptions import MethodNotAllowed, NotFound
        for adapter in self.adapter_maps:
            try:
                adapter.match(path, method="GET")
                return True
            except MethodNotAllowed:
                return True          # path exists, different verb
            except NotFound:
                continue
            except Exception:
                continue
        return False

    def test_node_agent_calls_only_paths_the_cloud_serves(self):
        missing = {p: sorted(w) for p, w in _python_client_paths().items()
                   if not self._routable(p)}
        self.assertEqual(
            missing, {},
            "the node agent calls cloud endpoints that do not exist — these "
            "fail silently in the field:\n"
            + "\n".join(f"  {p}  (in {', '.join(w)})" for p, w in missing.items()))

    def test_desktop_app_calls_only_paths_the_cloud_serves(self):
        missing = {p: sorted(w) for p, w in _dart_client_paths().items()
                   if not self._routable(p)}
        self.assertEqual(
            missing, {},
            "the desktop app calls cloud endpoints that do not exist:\n"
            + "\n".join(f"  {p}  (in {', '.join(w)})" for p, w in missing.items()))

    def test_no_client_calls_a_retired_endpoint(self):
        retired = _retired_paths()
        clients = {**_python_client_paths(), **_dart_client_paths()}
        offenders = {}
        for path, where in clients.items():
            for gone in retired:
                # Compare against the rule template with its converters
                # collapsed, so /x/<int:id> matches a called /x/1.
                pattern = re.sub(r"<[^>]+>", _PLACEHOLDER, gone)
                if path.rstrip("/") == pattern.rstrip("/"):
                    offenders[path] = sorted(where)
        self.assertEqual(
            offenders, {},
            "a client still calls an endpoint the cloud has retired (410 "
            "Gone) — this is the failure that made the iOS app unable to "
            "link a telescope:\n"
            + "\n".join(f"  {p}  (in {', '.join(w)})"
                        for p, w in offenders.items()))

    def test_the_check_itself_finds_the_retired_endpoints(self):
        """Guard the guard: if 410-detection silently stopped working, both
        tests above would pass vacuously."""
        retired = _retired_paths()
        self.assertIn("/api/v1/me/activation-code", retired)
        self.assertTrue(len(retired) >= 2, retired)

    def test_the_check_itself_rejects_endpoints_that_do_not_exist(self):
        """Guard the guard. The cloud serves the marketing site from a
        `/<path:filename>` catch-all that matches every URL, so a naive
        router check reports every path — including deleted ones — as
        routable, and the drift tests above pass no matter what."""
        self.assertTrue(self._routable("/api/v1/nodes/heartbeat"),
                        "a real POST-only endpoint must count as routable")
        self.assertFalse(self._routable("/api/v1/nodes/vanished"),
                         "a nonexistent endpoint must NOT count as routable — "
                         "the website catch-all is masking the check")

    def test_the_check_itself_sees_real_client_calls(self):
        node = _python_client_paths()
        app = _dart_client_paths()
        self.assertIn("/api/v1/nodes/heartbeat", node)
        self.assertIn("/api/v1/me/nodes/attach", app)


if __name__ == "__main__":
    unittest.main()
