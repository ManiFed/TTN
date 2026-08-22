"""The patrol: find the problem, gather the evidence, hand it over.

This is the half of the loop that does not need judgement. It runs the fleet
integrity checks on a schedule, and when something is wrong it assembles a
report an agent (or a person) can act on without having to re-derive what
happened: the finding, what it means, where the code lives, and how to
reproduce it.

It deliberately does not open the pull request itself. Writing the fix is the
part that needs judgement, and `scripts/merge_policy.py` decides what may land
unattended. What this guarantees is that whoever does write the fix starts from
evidence rather than from a guess -- which is the whole reason the loop is
worth having, since the alternative is a plausible-sounding patch for a bug
nobody actually observed.

    python -m telescope_mcp.patrol                 # human-readable
    python -m telescope_mcp.patrol --json          # machine-readable
    python -m telescope_mcp.patrol --exit-code     # non-zero if anything found
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .client import ApiError, CloudClient

#: Where each finding's cause lives, and what to look at first. Keeps whoever
#: picks the finding up from starting with a repository-wide search.
WHERE_TO_LOOK: dict[str, dict[str, str]] = {
    "orphaned_node": {
        "code": "cloud/registry.py (register_node, rekey_node), "
                "cloud/server.py (/api/v1/me/nodes/attach)",
        "means": "A node has no owning member. It keeps observing and keeps "
                 "producing science, but nobody can see it — which is why this "
                 "class of bug survived to production twice.",
        "reproduce": "tests/test_fleet_integrity.py::"
                     "FleetIntegrityTest::test_orphaned_node_with_history_is_critical",
    },
    "dangling_membership": {
        "code": "cloud/db.py (node_members), cloud/server.py account deletion",
        "means": "A member is linked to a node row that no longer exists.",
        "reproduce": "tests/test_fleet_integrity.py::"
                     "FleetIntegrityTest::test_dangling_membership_is_detected",
    },
    "missing_credentials": {
        "code": "cloud/registry.py (rekey_node), "
                "src/cloud_communicator.py (_handle_unauthorized, _rekey)",
        "means": "A node row has no usable api_key, so the node cannot "
                 "authenticate and will loop on rekey attempts forever.",
        "reproduce": "tests/test_fleet_integrity.py::"
                     "FleetIntegrityTest::test_missing_credentials_is_detected",
    },
    "stale_vacation": {
        "code": "cloud/registry.py (effective_status, heartbeat), "
                "cloud/nightly.py",
        "means": "The stored status disagrees with the calendar. It only flips "
                 "on the next heartbeat, so a node that stopped checking in can "
                 "advertise a vacation that ended months ago.",
        "reproduce": "tests/test_fleet_integrity.py::"
                     "FleetIntegrityTest::test_stale_vacation_is_detected",
    },
    "heartbeat_gap": {
        "code": "src/cloud_communicator.py (_heartbeat_loop)",
        "means": "A node believes it is observing but has gone quiet. The "
                 "agent's heartbeat thread may have died silently — an "
                 "exception in that loop used to kill it for good.",
        "reproduce": "tests/test_fleet_integrity.py::"
                     "FleetIntegrityTest::test_dead_heartbeat_thread_is_detected",
    },
    "ghost_registration": {
        "code": "cloud/server.py (/api/v1/nodes/register, /me/nodes/attach)",
        "means": "Registered and never checked in. One is a member who gave up "
                 "part-way through linking a telescope; a rising count means "
                 "the onboarding path is broken again.",
        "reproduce": "tests/test_fleet_integrity.py::"
                     "FleetIntegrityTest::test_ghost_registration_is_detected",
    },
    "duplicate_link": {
        "code": "cloud/server.py (/api/v1/me/nodes/attach)",
        "means": "One telescope claimed by several accounts. Legitimate for a "
                 "shared school telescope; a burst of them is not.",
        "reproduce": "tests/test_fleet_integrity.py::"
                     "FleetIntegrityTest::test_findings_are_ordered_critical_first",
    },
}

#: Findings at or above this severity are worth waking someone for.
ACTIONABLE = ("critical",)


def run(client: CloudClient | None = None) -> dict:
    """Run the sweep and return a report."""
    client = client or CloudClient()
    started = datetime.now(timezone.utc).isoformat()

    try:
        integrity = client.get("/admin/fleet-integrity", admin=True)
    except ApiError as exc:
        return {
            "ran_at": started,
            "ok": False,
            "error": f"Could not run the integrity check: {exc.message}",
            "hint": ("This needs an admin key. Set TELESCOPE_MCP_ADMIN_KEY."
                     if exc.status == 401 else
                     "Check TELESCOPE_MCP_CLOUD_BASE and that the API is up."),
            "findings": [],
        }

    findings = integrity.get("findings", [])
    for finding in findings:
        finding["context"] = WHERE_TO_LOOK.get(finding.get("check"), {})

    critical = [f for f in findings if f.get("severity") in ACTIONABLE]

    return {
        "ran_at": started,
        "ok": True,
        "healthy": bool(integrity.get("healthy")),
        "counts": integrity.get("counts", {}),
        "total_findings": len(findings),
        "actionable": len(critical),
        "findings": findings,
        "check_errors": integrity.get("errors", []),
        "report": render(findings, integrity),
    }


def render(findings: list[dict], integrity: dict) -> str:
    """A pull-request body: what broke, what it means, and how to prove it."""
    if not findings and not integrity.get("errors"):
        return ("Fleet integrity: clean. No orphaned nodes, no stale statuses, "
                "no silent heartbeat gaps.")

    lines = ["## Fleet integrity findings", ""]
    counts = integrity.get("counts", {})
    if counts:
        lines.append("  ".join(f"**{n} {sev}**" for sev, n in counts.items()))
        lines.append("")

    for finding in findings:
        context = finding.get("context") or {}
        lines.append(f"### `{finding.get('check')}` — {finding.get('severity')}")
        lines.append("")
        lines.append(f"- **Node:** `{finding.get('node_id')}`")
        lines.append(f"- **Detail:** {finding.get('detail')}")
        if context.get("means"):
            lines.append(f"- **Why it matters:** {context['means']}")
        if context.get("code"):
            lines.append(f"- **Where to look:** {context['code']}")
        if context.get("reproduce"):
            lines.append(f"- **Covering test:** `{context['reproduce']}`")
        extra = {k: v for k, v in finding.items()
                 if k not in ("check", "severity", "node_id", "detail", "context")}
        if extra:
            lines.append(f"- **Evidence:** `{json.dumps(extra, default=str)}`")
        lines.append("")

    for err in integrity.get("errors", []):
        lines.append(f"### check `{err.get('check')}` failed to run")
        lines.append("")
        lines.append(f"`{err.get('error')}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Found by `python -m telescope_mcp.patrol`. Reproduce the checks with "
        "`python -m pytest tests/test_fleet_integrity.py`.")
    lines.append("")
    lines.append(
        "Any fix here touches `cloud/registry.py`, `cloud/db.py` or "
        "`src/cloud_communicator.py`, all of which "
        "`scripts/merge_policy.py` protects — so it needs a person to read it "
        "before it merges, whoever writes it.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--exit-code", action="store_true",
                        help="exit non-zero when anything actionable is found")
    args = parser.parse_args()

    result = run()
    print(json.dumps(result, indent=2, default=str) if args.json
          else (result.get("report") or result.get("error", "")))

    if not result["ok"]:
        return 2
    if args.exit_code and result["actionable"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
