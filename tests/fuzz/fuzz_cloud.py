"""Cloud API fuzzer.

Boots the real Flask app (cloud/server.py) against a real ephemeral PostgreSQL
(tests/fuzz/pgtmp.py), registers genuine node/user identities, then fires
seeded, mutated requests at every /api route. Outbound network is disabled by
patching requests.Session.request to raise ConnectionError — which doubles as
fault injection for every code path that calls an external service.

Invariants:
  * No request may produce a 5xx (garbage in → 4xx, never a server error).
  * No NaN/Infinity persisted into numeric columns of key tables.
  * The read-side pipeline (light_curve/consensus) stays runnable afterwards.

Usage:
  venv/bin/python -m tests.fuzz.fuzz_cloud --requests 20000 --seed 1
  venv/bin/python -m tests.fuzz.fuzz_cloud --replay sim_results/fuzz_cloud/<run>/failures.jsonl:3
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import string
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Routes never fuzzed: static site serving and endpoints that would hang or
# talk to the outside world by design.
_SKIP_RULES = {
    "/", "/dashboard", "/dashboard/", "/<path:filename>",
    "/dashboard/<path:filename>",
    "/download/node-agent", "/download/node-agent/<platform>",
    "/static/<path:filename>",
}

_INTERESTING_STRINGS = [
    "", " ", "null", "None", "NaN", "Infinity", "-Infinity",
    "0", "-1", "1e308", "T CrB", "SS Cyg", "../../etc/passwd",
    "'; DROP TABLE nodes; --", "\x00", "𝕬𝖓𝖙𝖆𝖗𝖊𝖘", "a" * 5000,
    "%s", "{}", "[]", "\n\r\n", "<script>alert(1)</script>",
]
_INTERESTING_NUMBERS = [
    0, -1, 1, 2**31, 2**63, -2**63, 1e308, -1e308, 1e-308,
    float("nan"), float("inf"), float("-inf"), 3.14, -0.0, 99999999,
]


def _rand_scalar(rng: random.Random):
    r = rng.random()
    if r < 0.35:
        return rng.choice(_INTERESTING_STRINGS)
    if r < 0.7:
        return rng.choice(_INTERESTING_NUMBERS)
    if r < 0.8:
        return rng.choice([True, False, None])
    if r < 0.9:
        return [_rand_scalar(rng) for _ in range(rng.randint(0, 3))]
    return {rng.choice(string.ascii_lowercase): _rand_scalar(rng)}


def mutate(payload, rng: random.Random, n_mutations: int | None = None):
    """Return a mutated deep copy of a JSON-ish payload."""
    obj = json.loads(json.dumps(payload, default=str))
    if not isinstance(obj, dict) or not obj:
        return _rand_scalar(rng)
    keys = list(obj.keys())
    for _ in range(n_mutations if n_mutations is not None else rng.randint(1, 4)):
        op = rng.random()
        k = rng.choice(keys)
        if op < 0.3:
            obj.pop(k, None)                       # drop a key
        elif op < 0.75:
            obj[k] = _rand_scalar(rng)             # scramble a value
        elif op < 0.9:
            obj["".join(rng.choices(string.ascii_lowercase, k=6))] = _rand_scalar(rng)
        else:
            sub = obj.get(k)
            if isinstance(sub, dict) and sub:
                obj[k] = mutate(sub, rng, 1)       # recurse
            else:
                obj[k] = {"nested": _rand_scalar(rng)}
    return obj


# ── App bootstrap ──────────────────────────────────────────────────────────────

def _kill_outbound_network():
    import requests
    import requests.sessions

    def _blocked(self, method, url, *a, **kw):
        raise requests.ConnectionError(f"fuzz: outbound network disabled ({method} {url})")

    requests.sessions.Session.request = _blocked


def build_app(dsn: str, data_dir: Path):
    os.environ["DATABASE_URL"] = dsn
    _kill_outbound_network()
    from cloud import db
    db.init(dsn)
    import cloud.server as server
    config = {
        "server": {"admin_key": "fuzz-admin-key"},
        "storage": {
            "aavso_file_dir": str(data_dir / "aavso_files"),
            "mpc_file_dir": str(data_dir / "mpc_files"),
            "image_dir": str(data_dir / "images"),
        },
    }
    app = server.create_app(config)
    app.config["TESTING"] = False        # exercise real error handling
    # Disable the pairing-claim rate limiter's memory of the fuzz client:
    server._PAIR_CLAIM_MAX_MISSES = 10**9
    return app


class Identities:
    """Real credentials created through the production code paths."""

    def __init__(self, client):
        from cloud import registry, auth
        self.node = registry.register_node({
            "latitude": 31.5, "longitude": -99.2, "elevation": 500,
            "owner_name": "Fuzz Owner", "owner_email": "fuzz@example.org",
        })
        self.node2 = registry.register_node({
            "latitude": -33.9, "longitude": 18.4,
        })
        self.user = auth.register("fuzz-user@example.org", "hunter2hunter2", "Fuzz User")
        self.admin_key = "fuzz-admin-key"

    def headers(self, mode: str, rng: random.Random) -> dict:
        if mode == "node":
            return {"X-Node-Id": self.node["node_id"], "X-Api-Key": self.node["api_key"]}
        if mode == "user":
            return {"Authorization": f"Bearer {self.user['token']}"}
        if mode == "admin":
            return {"X-Admin-Key": self.admin_key}
        if mode == "garbage":
            def hv(s: str) -> str:
                # HTTP header values cannot carry newlines/NULs on the wire;
                # the test client enforces that before the app ever sees them.
                return s.replace("\n", " ").replace("\r", " ").replace("\x00", " ")
            return {
                "X-Node-Id": hv(rng.choice(_INTERESTING_STRINGS)),
                "X-Api-Key": hv(rng.choice(_INTERESTING_STRINGS)),
                "Authorization": "Bearer " + hv(rng.choice(_INTERESTING_STRINGS)),
                "X-Admin-Key": hv(rng.choice(_INTERESTING_STRINGS)),
            }
        return {}


# ── Valid payload templates for the high-value endpoints ───────────────────────

def _valid_measurement(rng: random.Random) -> dict:
    return {
        "target_name": rng.choice(["T CrB", "SS Cyg", "R Sct", "AE Aqr"]),
        "bjd": 2460871.0 + rng.random() * 30,
        "magnitude": 8.0 + rng.random() * 6,
        "uncertainty": 0.01 + rng.random() * 0.2,
        "filter": rng.choice(["V", "B", "CV", "TG"]),
        "airmass": 1.0 + rng.random() * 2,
        "fwhm": 2.0 + rng.random() * 4,
        "snr": 20 + rng.random() * 200,
        "comparison_stars": rng.randint(1, 12),
        "quality_flag": rng.choice(["good", "acceptable", "poor"]),
        "zero_point": 21.0 + rng.random(),
        "zp_scatter": rng.random() * 0.1,
        "fits_file": "frame_0001.fits",
        "sky_mag": 19.0 + rng.random() * 3,
    }


def _templates(rng: random.Random) -> dict:
    """endpoint-path → callable returning a plausible valid body."""
    return {
        "/api/v1/nodes/heartbeat": lambda: {
            "conditions": {"sky_temp_c": -18.0, "clouds": rng.random()},
            "clock_skew_s": rng.random(),
            "clock_qualified": True,
            "heartbeat_s": 60.0,
            "state": {"phase": rng.choice(["idle", "slewing", "exposing", "parked"]),
                      "target": "T CrB", "detail": "ok"},
        },
        "/api/v1/measurements": lambda: {"measurement": _valid_measurement(rng)},
        "/api/v1/incidents": lambda: {
            "incident_type": rng.choice(["slew_failed", "focus_failed", "camera_timeout"]),
            "severity": rng.choice(["info", "warning", "error"]),
            "target_name": "T CrB",
            "detail": {"timeout_s": 180},
        },
        "/api/v1/nodes/register": lambda: {
            "latitude": rng.uniform(-89, 89), "longitude": rng.uniform(-179, 179),
            "owner_name": "F", "owner_email": "f@example.org",
        },
        "/api/v1/nodes/pair": lambda: {"node_name": "Fuzz Scope"},
        "/api/v1/nodes/characterization": lambda: {
            "fov_deg": 1.2, "pixel_scale_arcsec": 2.4, "mag_faint_limit": 15.0},
        "/api/v1/nodes/execution-outcomes": lambda: {
            "outcomes": [{"attempt_id": f"att_{rng.randrange(10**6)}",
                          "state": rng.choice(["received", "started", "completed",
                                               "skipped", "failed", "cancelled"]),
                          "bundle_id": "bundle_x", "item_id": "item_1",
                          "task_id": "task_1", "frames_attempted": 20,
                          "frames_completed": 18, "failure_reason": "",
                          "detail": {"k": 1}}]},
        "/api/v1/survey": lambda: {
            "frame": {"bjd": 2460871.5, "fits_file": "s.fits"},
            "sources": [{"ra_deg": 150.1, "dec_deg": 2.2, "mag": 14.2,
                         "mag_err": 0.05, "fwhm": 3.0}]},
        "/api/v1/auth/register": lambda: {
            "email": f"u{rng.randrange(10**9)}@example.org",
            "password": "pw12345678", "display_name": "U"},
        "/api/v1/auth/login": lambda: {
            "email": "fuzz-user@example.org", "password": "hunter2hunter2"},
        "/api/v1/subscribe": lambda: {"email": f"s{rng.randrange(10**9)}@example.org"},
        "/api/v1/me/nodes/attach": lambda: {"pairing_token": "APPLE-1234"},
        "/api/v1/interrupts": lambda: {
            "node_id": "node_x", "target_name": "T CrB", "reason": "transient",
            "priority": rng.random()},
        "/api/v1/me/science-program-suggestions": lambda: {"text": "observe more novae"},
        "/api/v1/me/contributions": lambda: {
            "filename": "img.fits", "sha256": "0" * 64, "size_bytes": 1024},
    }


# ── Path parameter filling ─────────────────────────────────────────────────────

def _fill_path(rule: str, rng: random.Random, ids: Identities) -> str:
    out = []
    for part in rule.split("/"):
        if part.startswith("<"):
            conv = part.strip("<>")
            if conv.startswith("int:"):
                out.append(str(rng.choice([0, 1, 7, 999999, 2**31])))
            elif "node_id" in conv:
                out.append(rng.choice([ids.node["node_id"], "node_missing", "x"]))
            elif "target_name" in conv or "object_name" in conv:
                out.append(rng.choice(["T CrB", "SS%20Cyg", "no_such", "..%2f..%2fetc"]))
            elif "token" in conv:
                out.append(rng.choice(["APPLE-1234", "ZZZZZ-0000", "x"]))
            else:
                out.append(rng.choice(["x", "1", "no_such", "a%2fb", "model_v1"]))
        else:
            out.append(part)
    return "/".join(out)


# ── Invariants ─────────────────────────────────────────────────────────────────

_NAN_CHECKS = [
    ("measurements", ["magnitude", "uncertainty", "bjd", "airmass", "snr"]),
    ("nodes", ["latitude", "longitude", "reliability_score", "mean_uncertainty"]),
    ("targets", ["ra_deg", "dec_deg", "mag", "priority"]),
    ("scores", ["total"]),
]


def check_db_invariants() -> list[str]:
    from cloud import db
    problems = []
    for table, cols in _NAN_CHECKS:
        for col in cols:
            try:
                rows = db.query(
                    f"SELECT count(*) AS n FROM {table} "
                    f"WHERE {col} = 'NaN'::float8 OR {col} = 'Infinity'::float8 "
                    f"OR {col} = '-Infinity'::float8")
                if rows and rows[0]["n"]:
                    problems.append(f"non-finite values persisted: {table}.{col} ({rows[0]['n']} rows)")
            except Exception as exc:
                problems.append(f"invariant query failed on {table}.{col}: {exc}")
    return problems


def check_pipeline_alive() -> list[str]:
    from cloud import data_pipeline
    problems = []
    try:
        data_pipeline.light_curve("T CrB")
        data_pipeline.consensus_light_curve("T CrB")
    except Exception as exc:
        problems.append(f"read pipeline crashed after fuzzing: {type(exc).__name__}: {exc}")
    return problems


# ── Main loop ──────────────────────────────────────────────────────────────────

def run(n_requests: int, seed: int, out_dir: Path, valid_ratio: float = 0.35) -> int:
    from tests.fuzz.pgtmp import TempPostgres

    out_dir.mkdir(parents=True, exist_ok=True)
    failures_path = out_dir / "failures.jsonl"
    seen_signatures: set = set()
    n_fail = 0
    status_hist: dict = {}

    with TempPostgres() as pg:
        app = build_app(pg.dsn, out_dir / "data")
        client = app.test_client()
        ids = Identities(client)
        rng = random.Random(seed)
        templates = _templates(rng)

        rules = [r for r in app.url_map.iter_rules()
                 if str(r) not in _SKIP_RULES and str(r).startswith("/api")]
        api_rules = []
        for r in rules:
            for m in (r.methods or set()) - {"HEAD", "OPTIONS"}:
                api_rules.append((m, str(r)))
        print(f"[fuzz_cloud] seed={seed} targeting {len(api_rules)} route/method pairs")

        t0 = time.time()
        for i in range(n_requests):
            method, rule = rng.choice(api_rules)
            path = _fill_path(rule, rng, ids)
            auth_mode = rng.choices(
                ["node", "user", "admin", "none", "garbage"],
                weights=[0.35, 0.25, 0.1, 0.15, 0.15])[0]
            headers = ids.headers(auth_mode, rng)

            body = None
            if method in ("POST", "PUT", "PATCH"):
                template = templates.get(rule)
                if template and rng.random() < valid_ratio:
                    body = template()
                elif template:
                    body = mutate(template(), rng)
                else:
                    body = _rand_scalar(rng)

            kwargs: dict = {"headers": headers}
            if body is not None:
                wire = rng.random()
                if wire < 0.9:
                    kwargs["json"] = json.loads(json.dumps(body, default=str)
                                                ) if _finite(body) else None
                    if kwargs["json"] is None:
                        kwargs["data"] = json.dumps(body, default=str, allow_nan=True)
                        kwargs["content_type"] = "application/json"
                        del kwargs["json"]
                elif wire < 0.95:
                    kwargs["data"] = json.dumps(body, default=str, allow_nan=True)[:-1]
                    kwargs["content_type"] = "application/json"     # truncated JSON
                else:
                    kwargs["data"] = os.urandom(rng.randint(0, 256))
                    kwargs["content_type"] = rng.choice(
                        ["application/json", "text/plain", "application/octet-stream"])

            try:
                resp = client.open(path, method=method, **kwargs)
                status = resp.status_code
                text = resp.get_data(as_text=True)[:500]
            except Exception as exc:
                status = -1
                text = f"{type(exc).__name__}: {exc}"

            status_hist[status] = status_hist.get(status, 0) + 1
            if status >= 500 or status == -1:
                sig = (rule, method, status, text.splitlines()[0][:120] if text else "")
                n_fail += 1
                if sig not in seen_signatures:
                    seen_signatures.add(sig)
                    record = {
                        "seed": seed, "i": i, "method": method, "rule": rule,
                        "path": path, "auth": auth_mode, "status": status,
                        "body": json.dumps(body, default=str, allow_nan=True) if body is not None else None,
                        "response": text,
                    }
                    with failures_path.open("a") as fh:
                        fh.write(json.dumps(record) + "\n")
                    print(f"  FAIL [{status}] {method} {path} (auth={auth_mode}) — {text.splitlines()[0][:120]}")

            if i and i % 5000 == 0:
                rate = i / (time.time() - t0)
                print(f"  {i}/{n_requests} ({rate:.0f} req/s), {n_fail} failures, "
                      f"{len(seen_signatures)} unique")

        problems = check_db_invariants() + check_pipeline_alive()
        for p in problems:
            print(f"  INVARIANT VIOLATION: {p}")
            with failures_path.open("a") as fh:
                fh.write(json.dumps({"seed": seed, "invariant": p}) + "\n")

        elapsed = time.time() - t0
        print(f"[fuzz_cloud] done: {n_requests} requests in {elapsed:.0f}s "
              f"({n_requests/max(elapsed,1e-9):.0f} req/s)")
        print(f"[fuzz_cloud] status histogram: "
              + ", ".join(f"{k}:{v}" for k, v in sorted(status_hist.items())))
        print(f"[fuzz_cloud] {n_fail} failing requests, "
              f"{len(seen_signatures)} unique signatures, "
              f"{len(problems)} invariant violations → {failures_path}")
        return 1 if (seen_signatures or problems) else 0


def _finite(obj) -> bool:
    """True if obj contains no NaN/Inf floats (json.dumps would emit invalid JSON)."""
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_finite(v) for v in obj)
    return True


def replay(spec: str) -> int:
    path_str, _, idx_str = spec.partition(":")
    records = [json.loads(line) for line in Path(path_str).read_text().splitlines()]
    record = records[int(idx_str or 0)]
    print(json.dumps(record, indent=2))

    from tests.fuzz.pgtmp import TempPostgres
    out = Path("sim_results/fuzz_cloud/replay")
    with TempPostgres() as pg:
        app = build_app(pg.dsn, out / "data")
        client = app.test_client()
        ids = Identities(client)
        headers = ids.headers(record.get("auth", "none"), random.Random(0))
        kwargs: dict = {"headers": headers}
        if record.get("body") is not None:
            kwargs["data"] = record["body"]
            kwargs["content_type"] = "application/json"
        resp = client.open(record["path"], method=record["method"], **kwargs)
        print(f"→ HTTP {resp.status_code}")
        print(resp.get_data(as_text=True)[:2000])
        return 0 if resp.status_code < 500 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--requests", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path,
                    default=Path("sim_results/fuzz_cloud") / time.strftime("%Y%m%d_%H%M%S"))
    ap.add_argument("--replay", help="failures.jsonl:INDEX to re-send one request")
    args = ap.parse_args()
    if args.replay:
        return replay(args.replay)
    return run(args.requests, args.seed, args.out)


if __name__ == "__main__":
    sys.exit(main())
