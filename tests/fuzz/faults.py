"""Seed-driven fault plans for the fake ALPACA observatory.

A FaultPlan is generated deterministically from a seed and describes both
*per-request* fault probabilities (transport/protocol/semantic classes) and
*behavioral episodes* (a device goes bad for a stretch of wall-clock time).
The plan serializes to JSON so any failing run can be replayed exactly.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field, asdict

# Per-request fault kinds (applied to a single HTTP exchange)
TRANSPORT_FAULTS = ["http500", "hang", "drop", "truncated_json"]
PROTOCOL_FAULTS = ["error_number", "missing_value", "wrong_type_value", "non_json"]
SEMANTIC_FAULTS = ["nan_coords", "out_of_range_coords", "negative_number", "huge_number"]

# Behavioral episode kinds (device-level state for a duration)
BEHAVIORAL_FAULTS = [
    "device_down",        # connection refused on every request
    "slewing_stuck",      # telescope reports slewing=True forever
    "park_raises",        # park returns ErrorNumber, atpark never True
    "camera_never_ready", # imageready stays False
    "filter_stuck",       # filterwheel reports position=-1 forever
    "atpark_flapping",    # atpark alternates every read
    "device_reboot",      # device state resets mid-scenario
    "commands_fail",      # GETs fine, PUTs all return ErrorNumber (partial failure)
]


@dataclass
class Episode:
    start_s: float
    duration_s: float
    device: str          # telescope | camera | filterwheel | focuser | covercalibrator | all
    kind: str

    def active(self, elapsed: float) -> bool:
        return self.start_s <= elapsed < self.start_s + self.duration_s


@dataclass
class FaultPlan:
    seed: int = 0
    # probability that any given request suffers a fault of each class
    p_transport: float = 0.0
    p_protocol: float = 0.0
    p_semantic: float = 0.0
    episodes: list = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        d["episodes"] = [asdict(e) if isinstance(e, Episode) else e
                         for e in self.episodes]
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "FaultPlan":
        d = json.loads(text)
        d["episodes"] = [Episode(**e) for e in d.get("episodes", [])]
        return cls(**d)

    @classmethod
    def generate(cls, seed: int, scenario_s: float = 30.0,
                 profile: str = "mixed") -> "FaultPlan":
        """Deterministic plan from a seed.

        profile: "none" (baseline), "transport", "protocol", "semantic",
                 "behavioral", "mixed".
        """
        rng = random.Random(seed)
        plan = cls(seed=seed)
        if profile == "none":
            return plan
        heavy = profile == "heavy"
        if profile in ("transport", "mixed") or heavy:
            plan.p_transport = rng.choice([0.1, 0.2, 0.35]) if heavy else \
                rng.choice([0.0, 0.02, 0.05, 0.15])
        if profile in ("protocol", "mixed") or heavy:
            plan.p_protocol = rng.choice([0.1, 0.2, 0.3]) if heavy else \
                rng.choice([0.0, 0.02, 0.05, 0.1])
        if profile in ("semantic", "mixed") or heavy:
            plan.p_semantic = rng.choice([0.1, 0.2, 0.3]) if heavy else \
                rng.choice([0.0, 0.02, 0.05, 0.1])
        if profile in ("behavioral", "mixed") or heavy:
            for _ in range(rng.randint(2, 5) if heavy else rng.randint(0, 3)):
                kind = rng.choice(BEHAVIORAL_FAULTS)
                device = rng.choice(
                    ["telescope", "camera", "filterwheel", "all"]
                    if kind in ("device_down", "device_reboot", "commands_fail")
                    else {"slewing_stuck": ["telescope"],
                          "park_raises": ["telescope"],
                          "atpark_flapping": ["telescope"],
                          "camera_never_ready": ["camera"],
                          "filter_stuck": ["filterwheel"]}[kind])
                plan.episodes.append(Episode(
                    start_s=round(rng.uniform(0, scenario_s * 0.7), 2),
                    duration_s=round(rng.uniform(1.0, scenario_s * 0.6), 2),
                    device=device, kind=kind))
        return plan


class FaultInjector:
    """Consulted by the FakeObservatory on every request.

    Thread-safe; the HTTP server handles requests from several node threads
    at once (poller, safety heartbeat, schedule runner).
    """

    def __init__(self, plan: FaultPlan):
        self.plan = plan
        self._rng = random.Random(plan.seed ^ 0x5EED)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def active_episodes(self, device: str) -> list:
        t = self.elapsed()
        return [e for e in self.plan.episodes
                if e.active(t) and e.device in (device, "all")]

    def request_fault(self) -> str | None:
        """Pick a per-request fault kind, or None for a clean exchange."""
        with self._lock:
            r = self._rng.random()
            if r < self.plan.p_transport:
                return self._rng.choice(TRANSPORT_FAULTS)
            r -= self.plan.p_transport
            if r < self.plan.p_protocol:
                return self._rng.choice(PROTOCOL_FAULTS)
            r -= self.plan.p_protocol
            if r < self.plan.p_semantic:
                return self._rng.choice(SEMANTIC_FAULTS)
        return None

    def rand(self) -> random.Random:
        return self._rng
