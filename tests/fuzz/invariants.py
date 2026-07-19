"""Invariant checks over a live (or just-finished) node harness run.

Each check returns a list of violation strings (empty = holds). The harness
runs them at teardown; the runner records any violation together with the
fault plan for replay.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

VALID_PHASES = {"", "starting", "waiting", "slewing", "exposing", "done", "cancelled"}

# Log lines that are expected under fault injection (the node *should* be
# logging errors when its hardware misbehaves) — an ERROR line only counts
# as a violation if it carries a traceback that is not on this allowlist.
_TRACEBACK_ALLOWLIST = [
    "AlpacaError",            # injected device errors surface as these
    "requests.exceptions",    # transport faults
    "ConnectionError",
    "ReadTimeout",
    "ConnectTimeout",
    "TimeoutError",
    "JSONDecodeError",        # truncated/non-JSON bodies
    "ExposureCancelled",
]


def check_threads(baseline: set[int], captured_exceptions: list) -> list[str]:
    """I2 — no daemon thread died with an unhandled exception."""
    problems = []
    for exc in captured_exceptions:
        problems.append(
            f"thread {exc['thread']} died: {exc['type']}: {exc['value']}")
    return problems


def check_phase(sched_state: dict) -> list[str]:
    """I3 — the phase machine ends in a valid, terminal-consistent state."""
    problems = []
    phase = sched_state.get("current_phase", "")
    if phase not in VALID_PHASES:
        problems.append(f"unknown schedule phase: {phase!r}")
    if sched_state.get("running"):
        problems.append(
            f"schedule still 'running' at teardown (phase={phase!r}) — wedged")
    return problems


def check_safety_parked(safety_status: dict, park_attempts: int) -> list[str]:
    """I1 — a latched-unsafe safety manager must have *attempted* a park.

    park_attempts counts park PUTs seen on the wire (including ones the fake
    failed): the node is judged on trying, not on the hardware cooperating.
    """
    problems = []
    if safety_status and not safety_status.get("safe", True):
        if park_attempts == 0:
            problems.append(
                "safety manager reports unsafe but no park command was ever "
                f"sent (reason: {safety_status.get('reason', '?')})")
    return problems


def check_poller_singleton() -> list[str]:
    """I6 — at most one alpaca-poller thread alive."""
    pollers = [t for t in threading.enumerate()
               if t.name == "alpaca-poller" and t.is_alive()]
    if len(pollers) > 1:
        return [f"{len(pollers)} concurrent alpaca-poller threads"]
    return []


_TB_RE = re.compile(r"Traceback \(most recent call last\)")


def check_log(log_path: Path) -> list[str]:
    """I5 — no unexplained tracebacks in the node log."""
    if not log_path.exists():
        return []
    problems = []
    text = log_path.read_text(errors="replace")
    blocks = text.split("Traceback (most recent call last)")
    for block in blocks[1:]:
        tail = block[:2000]
        if not any(allowed in tail for allowed in _TRACEBACK_ALLOWLIST):
            first_line = next(
                (ln.strip() for ln in reversed(tail.splitlines()[:15])
                 if ln.strip() and not ln.startswith(" ")), "?")
            problems.append(f"unexplained traceback in log: {first_line[:200]}")
    return problems


def check_outbox(fakecloud, outbox_dir: Path, enqueued: int) -> list[str]:
    """I4 — measurements are delivered or still queued, never dropped."""
    if enqueued == 0:
        return []
    delivered = fakecloud.count_paths("/measurements")
    pending = 0
    if outbox_dir.exists():
        pending = sum(1 for _ in outbox_dir.rglob("*") if _.is_file())
    if delivered + pending < enqueued:
        return [f"outbox lost data: {enqueued} enqueued, "
                f"{delivered} delivered, {pending} pending"]
    return []
