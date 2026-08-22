"""Nightly intent: what each telescope is doing tonight, and who decided.

Replaces "remember to set a vacation" with a question asked every day. Each
night a node gets a proposal -- research-weighted, because that is the point of
the network -- and one of four things happens:

  the member accepts it            -> status 'accepted'
  the member declines tonight      -> status 'declined'
  the member says nothing by dusk  -> status 'auto', the proposal runs
  the member stands the node down  -> status 'stood_down', effective at once

Two rules sit above all of that:

  Weather wins.  A proposal that would point an open telescope at rain is held
  regardless of who accepted it, and re-checked rather than cancelled -- a
  forecast four hours before dusk is not a forecast at dusk.

  Override wins faster.  A member standing their telescope down is an
  instruction, not a preference. It takes effect on the next SSE signal (~1s),
  never waits for a poll cycle, and is never overridden by a later auto-accept.

Silence means yes on purpose: a telescope that only observes when someone
remembers to press a button contributes almost nothing, and the members this
is built for should not have to.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db, registry

logger = logging.getLogger("cloud.nightly")

#: Statuses a night can be in.
PROPOSED = "proposed"
ACCEPTED = "accepted"
DECLINED = "declined"
AUTO = "auto"
STOOD_DOWN = "stood_down"
WEATHER_HOLD = "weather_hold"

#: Statuses that mean "observe tonight".
OBSERVING = (ACCEPTED, AUTO)

#: Statuses a member set deliberately. Never re-decided by the auto path.
MEMBER_DECIDED = (ACCEPTED, DECLINED, STOOD_DOWN)

#: How long before local dusk the proposal auto-accepts if nobody answered.
AUTO_ACCEPT_LEAD_MINUTES = 60

#: Cloud cover above this fraction means there is nothing to observe.
CLOUD_COVER_HOLD = 0.85

#: Any non-zero precipitation holds the night. A wet telescope is a broken one.
PRECIPITATION_HOLD = 0.0

#: Default length of a research run when nobody says otherwise.
DEFAULT_RESEARCH_HOURS = 4.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def tonight_date(node: dict) -> str:
    """The observing night's date in the node's own local time.

    A night is named for the evening it starts, so local afternoon and the
    small hours after midnight both belong to the same 'tonight'.
    """
    offset = float(node.get("utc_offset_hours") or 0.0)
    local = _now() + timedelta(hours=offset)
    if local.hour < 12:          # after midnight, still last evening's night
        local -= timedelta(days=1)
    return local.date().isoformat()


# ── proposals ────────────────────────────────────────────────────────────────

def build_proposal(node: dict) -> dict:
    """The recommendation for one node tonight.

    Research-weighted by default. Imaging is offered as a tail on the end of
    the night rather than as an alternative to research, so the default answer
    to "what am I doing tonight" is always science first.
    """
    hours = DEFAULT_RESEARCH_HOURS
    return {
        "mode": "research",
        "research_hours": hours,
        "imaging_after": True,
        "summary": (
            f"{hours:.0f} hours of network research photometry, then imaging "
            f"on a target of your choosing for the rest of the night."
        ),
        "why": (
            "Research runs are what the network exists for — your telescope's "
            "measurements go to AAVSO alongside everyone else's. Imaging after "
            "the research block costs the science nothing."
        ),
    }


# ── weather ──────────────────────────────────────────────────────────────────

def weather_verdict(node: dict, forecast: Optional[dict] = None) -> dict:
    """Whether tonight is observable at all.

    Fails open: an unavailable forecast must not stop a telescope observing,
    because the node's own SafetyManager is the real authority on local
    conditions and it can see the sky.
    """
    if forecast is None:
        try:
            from .conditions import fetch_astronomy_weather
            forecast = fetch_astronomy_weather(
                float(node.get("latitude") or 0.0),
                float(node.get("longitude") or 0.0),
            )
        except Exception as exc:
            logger.debug("Weather lookup failed for %s: %s", node.get("node_id"), exc)
            forecast = None

    if not forecast:
        return {"observable": True, "reason": "No forecast available; "
                "deferring to the node's own safety checks.", "source": "none"}

    cloud = forecast.get("cloud_cover")
    precip = forecast.get("precipitation")

    if precip is not None and float(precip) > PRECIPITATION_HOLD:
        return {"observable": False,
                "reason": f"Precipitation forecast ({precip}). Staying closed.",
                "source": "forecast"}

    if cloud is not None and float(cloud) > CLOUD_COVER_HOLD:
        return {"observable": False,
                "reason": f"Cloud cover forecast at {float(cloud) * 100:.0f}%. "
                          f"Nothing to measure.",
                "source": "forecast"}

    return {"observable": True, "reason": "Forecast is workable.",
            "source": "forecast"}


# ── persistence ──────────────────────────────────────────────────────────────

def _row(node_id: str, night: str) -> Optional[dict]:
    return db.query_one(
        "SELECT * FROM night_intents WHERE node_id = %s AND night = %s",
        (node_id, night),
    )


def _respond_by(node: dict) -> str:
    """When silence becomes consent: roughly an hour before local dusk.

    Dusk is approximated from the node's UTC offset rather than computed
    astronomically -- the node itself gates on real sun position, so this only
    has to be close enough to be a sensible deadline to show a member.
    """
    offset = float(node.get("utc_offset_hours") or 0.0)
    local = _now() + timedelta(hours=offset)
    dusk_local = local.replace(hour=20, minute=0, second=0, microsecond=0)
    if local.hour >= 20:
        dusk_local = local.replace(hour=23, minute=59, second=0, microsecond=0)
    deadline = dusk_local - timedelta(minutes=AUTO_ACCEPT_LEAD_MINUTES)
    return _iso(deadline - timedelta(hours=offset))


def get_or_create(node: dict) -> dict:
    """Tonight's intent for one node, proposing one if none exists yet."""
    node_id = node["node_id"]
    night = tonight_date(node)
    row = _row(node_id, night)
    if row:
        return _hydrate(row)

    proposal = build_proposal(node)
    db.execute(
        """INSERT INTO night_intents
               (node_id, night, status, proposal_json, respond_by, created_at)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (node_id, night) DO NOTHING""",
        (node_id, night, PROPOSED, json.dumps(proposal),
         _respond_by(node), _iso(_now())),
    )
    return _hydrate(_row(node_id, night))


def _hydrate(row: Optional[dict]) -> dict:
    if not row:
        return {}
    out = dict(row)
    out["proposal"] = db.loads(row.get("proposal_json"), {})
    out.pop("proposal_json", None)
    return out


# ── decisions ────────────────────────────────────────────────────────────────

def respond(node: dict, decision: str, research_hours: float | None = None,
            imaging_after: bool | None = None, note: str = "") -> dict:
    """Record a member's answer for tonight.

    `decision` is 'accept' or 'decline'. Accepting may adjust the shape of the
    run; declining is for tonight only, and does not touch later nights.
    """
    intent = get_or_create(node)
    night = intent["night"]

    if decision not in ("accept", "decline"):
        raise ValueError("decision must be 'accept' or 'decline'")

    status = ACCEPTED if decision == "accept" else DECLINED
    proposal = dict(intent.get("proposal") or {})
    if research_hours is not None:
        proposal["research_hours"] = max(0.0, float(research_hours))
    if imaging_after is not None:
        proposal["imaging_after"] = bool(imaging_after)

    db.execute(
        """UPDATE night_intents
              SET status = %s, proposal_json = %s, decided_at = %s,
                  decided_via = 'member', note = %s
            WHERE node_id = %s AND night = %s""",
        (status, json.dumps(proposal), _iso(_now()), note[:500],
         node["node_id"], night),
    )
    return _hydrate(_row(node["node_id"], night))


def stand_down(node: dict, reason: str = "", nights: int = 0) -> dict:
    """Stop observing now. The instant-override path.

    Takes effect on the node's next SSE signal rather than its next poll. With
    `nights` > 0 this also parks the node for that many further nights, which
    is the same mechanism as a vacation -- deliberately, so there is one way
    for a telescope to be out rather than two that can disagree.
    """
    intent = get_or_create(node)
    db.execute(
        """UPDATE night_intents
              SET status = %s, decided_at = %s, decided_via = 'override',
                  note = %s
            WHERE node_id = %s AND night = %s""",
        (STOOD_DOWN, _iso(_now()), (reason or "Member stood the node down.")[:500],
         node["node_id"], intent["night"]),
    )

    if nights and nights > 0:
        until = (_now() + timedelta(days=int(nights))).date().isoformat()
        registry.set_vacation(node["node_id"], until)

    return _hydrate(_row(node["node_id"], intent["night"]))


def hold_for_weather(node: dict, verdict: dict) -> dict:
    """Park tonight because of the forecast, without discarding the decision."""
    intent = get_or_create(node)
    # decided_via is deliberately left alone. A hold is a temporary state laid
    # over the member's decision, not a replacement for it -- overwriting it
    # here would lose the fact that they had accepted, and the night would come
    # back as merely 'proposed' when the sky cleared.
    db.execute(
        """UPDATE night_intents
              SET status = %s, decided_at = %s, note = %s
            WHERE node_id = %s AND night = %s""",
        (WEATHER_HOLD, _iso(_now()), str(verdict.get("reason", ""))[:500],
         node["node_id"], intent["night"]),
    )
    return _hydrate(_row(node["node_id"], intent["night"]))


# ── resolution ───────────────────────────────────────────────────────────────

def resolve(node: dict, check_weather: bool = True) -> dict:
    """What this node should actually do tonight, right now.

    Called by the planner and by the node itself. Applies, in order:
      1. a member override, which nothing else may overturn
      2. the weather, which overrules an acceptance but not a stand-down
      3. the deadline, after which silence becomes consent
    """
    intent = get_or_create(node)
    status = intent.get("status")

    # 1. A member's own decision to stop is final for tonight.
    if status == STOOD_DOWN:
        return _verdict(intent, observing=False,
                        reason=intent.get("note") or "Stood down by the member.")

    if status == DECLINED:
        return _verdict(intent, observing=False,
                        reason="Declined for tonight.")

    # 2. Weather overrules an acceptance, and is re-checked each time.
    if check_weather:
        verdict = weather_verdict(node)
        if not verdict["observable"]:
            if status != WEATHER_HOLD:
                intent = hold_for_weather(node, verdict)
            return _verdict(intent, observing=False, reason=verdict["reason"])
        if status == WEATHER_HOLD:
            # Forecast improved -- put the night back the way the member left
            # it. An accepted night resumes as accepted; one nobody answered
            # goes back to awaiting an answer, and the deadline below decides.
            restored = ACCEPTED if intent.get("decided_via") == "member" else PROPOSED
            db.execute(
                """UPDATE night_intents SET status = %s
                    WHERE node_id = %s AND night = %s""",
                (restored, node["node_id"], intent["night"]),
            )
            intent = _hydrate(_row(node["node_id"], intent["night"]))
            status = intent.get("status")

    # 3. Silence becomes consent once the deadline passes.
    if status == PROPOSED and _deadline_passed(intent):
        db.execute(
            """UPDATE night_intents
                  SET status = %s, decided_at = %s, decided_via = 'auto'
                WHERE node_id = %s AND night = %s AND status = %s""",
            (AUTO, _iso(_now()), node["node_id"], intent["night"], PROPOSED),
        )
        intent = _hydrate(_row(node["node_id"], intent["night"]))
        status = intent.get("status")

    if status in OBSERVING:
        return _verdict(intent, observing=True,
                        reason=("Running the recommended programme."
                                if status == AUTO else
                                "Accepted by the member."))

    return _verdict(intent, observing=False,
                    reason="Awaiting a decision; the recommendation will run "
                           "automatically if nobody answers.")


def _deadline_passed(intent: dict) -> bool:
    respond_by = intent.get("respond_by")
    if not respond_by:
        return True
    try:
        deadline = datetime.fromisoformat(str(respond_by))
    except ValueError:
        return True
    if not deadline.tzinfo:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return _now() >= deadline


def _verdict(intent: dict, observing: bool, reason: str) -> dict:
    return {
        "node_id": intent.get("node_id"),
        "night": intent.get("night"),
        "status": intent.get("status"),
        "observing": observing,
        "reason": reason,
        "decided_via": intent.get("decided_via") or "",
        "respond_by": intent.get("respond_by"),
        "proposal": intent.get("proposal") or {},
        "note": intent.get("note") or "",
    }


def observing_tonight(node: dict) -> bool:
    """Planner-facing shorthand: may this node be scheduled tonight?"""
    try:
        return bool(resolve(node)["observing"])
    except Exception as exc:
        # A failure here must not silently remove a working telescope from the
        # network -- the node's own safety checks remain in force either way.
        logger.warning("Nightly resolve failed for %s: %s — allowing the night",
                       node.get("node_id"), exc)
        return True
