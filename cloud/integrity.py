"""Fleet-integrity checks: the invariants the last several bug fixes restored.

Each check corresponds to a class of bug that reached production and was found
only because someone happened to look:

  orphaned_nodes        a node row with no owner — 319fded, 0c4bb87
  ghost_registrations   registered, never heartbeated — the duplicate-register path
  stale_vacation        status column lagging the calendar — 9421bbd, 2cbc6a1
  missing_credentials   a node row with no usable api_key — 5d926a1, 9280ba7
  heartbeat_gaps        an agent whose heartbeat thread died silently — 0c4bb87
  duplicate_links       one telescope claimed by several accounts

Checks are read-only and safe to run against production on any schedule. They
return findings rather than raising, so one broken invariant never hides the
rest. `run_all()` is what the admin endpoint and the MCP tool both call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db, registry

#: A node quiet for longer than this, while claiming to be observing, is a
#: finding rather than a blip. Deliberately well above HEARTBEAT_STALE_S (15
#: min) so a brief network drop does not register as a bug.
HEARTBEAT_GAP_HOURS = 6.0

#: A node that registered but never sent a single heartbeat after this long is
#: almost certainly an orphan from a failed onboarding attempt.
GHOST_AGE_HOURS = 24.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_hours(value) -> float | None:
    parsed = _parse(value)
    if parsed is None:
        return None
    return (_now() - parsed).total_seconds() / 3600.0


def _finding(check: str, severity: str, node_id: str, detail: str, **extra) -> dict:
    out = {"check": check, "severity": severity, "node_id": node_id, "detail": detail}
    out.update(extra)
    return out


# ── individual checks ────────────────────────────────────────────────────────

def orphaned_nodes() -> list[dict]:
    """Nodes with observation history but no owning member.

    A node that loses its node_members row keeps heartbeating and keeps
    producing science, but no longer appears on anyone's dashboard — the
    failure mode is silent, which is why it survived to production twice.
    """
    rows = db.query(
        """SELECT n.node_id, n.telescope_model, n.registered_at, n.last_heartbeat,
                  n.total_observations
           FROM nodes n
           LEFT JOIN node_members nm ON nm.node_id = n.node_id
           WHERE nm.node_id IS NULL"""
    )
    findings = []
    for row in rows:
        observations = int(row.get("total_observations") or 0)
        findings.append(_finding(
            "orphaned_node",
            "critical" if observations else "warning",
            row["node_id"],
            (f"Node has no owning member but has {observations} observations "
             f"on record." if observations else
             "Node has no owning member and no observations."),
            registered_at=row.get("registered_at"),
            last_heartbeat=row.get("last_heartbeat"),
            total_observations=observations,
        ))
    return findings


def ghost_registrations(age_hours: float = GHOST_AGE_HOURS) -> list[dict]:
    """Nodes that registered and then never checked in.

    Usually a member who got part-way through linking a telescope. Harmless
    individually, but a rising count means the onboarding path is broken again.
    """
    findings = []
    for row in db.query(
        "SELECT node_id, telescope_model, registered_at FROM nodes "
        "WHERE last_heartbeat IS NULL OR last_heartbeat = ''"
    ):
        age = _age_hours(row.get("registered_at"))
        if age is None or age < age_hours:
            continue
        findings.append(_finding(
            "ghost_registration", "warning", row["node_id"],
            f"Registered {age:.0f}h ago and has never sent a heartbeat.",
            registered_at=row.get("registered_at"),
            age_hours=round(age, 1),
        ))
    return findings


def stale_vacation() -> list[dict]:
    """Nodes whose stored status disagrees with the calendar.

    The status column only flips out of 'vacation' as a side effect of the next
    heartbeat, so a node that stops heartbeating can advertise a vacation that
    ended months ago. registry.effective_status() is the authority.
    """
    findings = []
    for row in db.query(
        "SELECT node_id, status, vacation_from, vacation_until FROM nodes "
        "WHERE status = 'vacation'"
    ):
        effective = registry.effective_status(row)
        if effective == row.get("status"):
            continue
        findings.append(_finding(
            "stale_vacation", "warning", row["node_id"],
            (f"Stored status is 'vacation' but the window "
             f"({row.get('vacation_from') or '?'} → {row.get('vacation_until') or '?'}) "
             f"has closed; effective status is '{effective}'."),
            stored_status=row.get("status"),
            effective_status=effective,
            vacation_until=row.get("vacation_until"),
        ))
    return findings


def missing_credentials() -> list[dict]:
    """Node rows with no usable API key — the node can never authenticate again."""
    findings = []
    for row in db.query(
        "SELECT node_id, registered_at FROM nodes WHERE api_key IS NULL OR api_key = ''"
    ):
        findings.append(_finding(
            "missing_credentials", "critical", row["node_id"],
            "Node row has no api_key; this node cannot authenticate and will "
            "loop on rekey attempts.",
            registered_at=row.get("registered_at"),
        ))
    return findings


def heartbeat_gaps(gap_hours: float = HEARTBEAT_GAP_HOURS) -> list[dict]:
    """Nodes that believe they are observing but have gone quiet.

    Excludes vacation, sleeping, disabled and contributor nodes — a sleeping
    portable node is quiet by design, and flagging one would recreate the
    false 'telescope missed last night' alert that vacationing members must
    never receive.
    """
    findings = []
    for row in db.query("SELECT * FROM nodes"):
        status = registry.effective_status(row)
        if status in ("vacation", "sleeping", "disabled", "contributor"):
            continue
        last = row.get("last_heartbeat")
        if not last:
            continue  # covered by ghost_registrations
        age = _age_hours(last)
        if age is None or age < gap_hours:
            continue
        findings.append(_finding(
            "heartbeat_gap", "warning", row["node_id"],
            f"Status is '{status}' but the last heartbeat was {age:.0f}h ago. "
            f"The agent's heartbeat thread may have died.",
            last_heartbeat=last,
            age_hours=round(age, 1),
            status=status,
        ))
    return findings


def duplicate_links() -> list[dict]:
    """One telescope claimed by more than one account.

    Legitimate for a shared school telescope, so this is informational — it is
    here because a burst of them means the linking flow is duplicating claims.
    """
    findings = []
    for row in db.query(
        """SELECT node_id, COUNT(*) AS n FROM node_members
           GROUP BY node_id HAVING COUNT(*) > 1"""
    ):
        findings.append(_finding(
            "duplicate_link", "info", row["node_id"],
            f"Claimed by {int(row['n'])} member accounts.",
            member_count=int(row["n"]),
        ))
    return findings


def dangling_memberships() -> list[dict]:
    """node_members rows pointing at a node row that no longer exists."""
    findings = []
    for row in db.query(
        """SELECT nm.node_id, nm.user_id FROM node_members nm
           LEFT JOIN nodes n ON n.node_id = nm.node_id
           WHERE n.node_id IS NULL"""
    ):
        findings.append(_finding(
            "dangling_membership", "critical", row["node_id"],
            "A member is linked to a node that no longer exists.",
            user_id=row.get("user_id"),
        ))
    return findings


#: Every check, in the order run_all reports them.
CHECKS = (
    ("orphaned_nodes", orphaned_nodes),
    ("dangling_memberships", dangling_memberships),
    ("missing_credentials", missing_credentials),
    ("stale_vacation", stale_vacation),
    ("heartbeat_gaps", heartbeat_gaps),
    ("ghost_registrations", ghost_registrations),
    ("duplicate_links", duplicate_links),
)

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def run_all() -> dict:
    """Run every check. A check that raises is reported, not fatal."""
    findings: list[dict] = []
    errors: list[dict] = []
    for name, fn in CHECKS:
        try:
            findings.extend(fn())
        except Exception as exc:  # a broken check must not hide the others
            errors.append({"check": name, "error": f"{type(exc).__name__}: {exc}"})

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9), f["check"]))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    return {
        "checked_at": _now().isoformat(),
        "checks_run": [name for name, _ in CHECKS],
        "healthy": not findings and not errors,
        "counts": counts,
        "total_findings": len(findings),
        "findings": findings,
        "errors": errors,
    }
