"""Tonight: deciding whether a telescope observes, one night at a time.

The old model asked a member to remember to set vacation dates before going
away. This one asks every day, recommends research, and runs the
recommendation if nobody answers -- so the default state of a telescope is
contributing, and opting out is a sentence rather than a form.

Tool results carry a `nudge`: a short, honest line about what the research
programme gets out of tonight. It is a nudge and not a gate. A member who
wants to image is not argued with, and declining is always one call away.
"""

from __future__ import annotations

from ..client import ApiError, CloudClient, encode_path
from ..guard import require_confirmation


def _nudge(verdict: dict, when: str = "tonight") -> str:
    """One line on where the night leaves the science. Never scolds."""
    proposal = verdict.get("proposal") or {}
    hours = proposal.get("research_hours")
    status = verdict.get("status")

    if status == "weather_hold":
        return ("Nothing to do about the weather. The night is held, not lost — "
                "if the forecast improves the run goes ahead automatically.")
    if status == "stood_down":
        return (f"Stood down. Nothing further will happen {when}; say the word "
                f"when you want it back and it picks up the same evening.")
    if status == "declined":
        return (f"Sitting {when} out. If you change your mind before dusk it "
                f"can still run — the research block is what feeds AAVSO.")
    if verdict.get("observing") and hours:
        return (f"{float(hours):.0f} hours of photometry {when} goes into the "
                f"network's light curves alongside everyone else's. Imaging "
                f"after the research block costs the science nothing.")
    return ("No answer needed — the research programme runs by default at dusk. "
            "Say so if you would rather it did not.")


def _with_nudge(verdict: dict, when: str = "tonight") -> dict:
    out = dict(verdict)
    out["nudge"] = _nudge(verdict, when)
    return out


def register(server, client: CloudClient) -> None:

    @server.tool()
    def tonight(node_id: str = "") -> dict:
        """What each telescope is doing tonight, and whether anyone decided it.

        Without node_id, covers every telescope on the account. Shows the
        recommendation, the deadline after which it runs on its own, and the
        current weather verdict.
        """
        if node_id:
            return _with_nudge(client.get(f"/me/nodes/{encode_path(node_id)}/tonight"))

        nodes = client.get("/me/nodes").get("nodes", [])
        out = []
        for node in nodes:
            nid = node.get("node_id")
            try:
                out.append(_with_nudge(
                    client.get(f"/me/nodes/{encode_path(nid)}/tonight")))
            except ApiError as exc:
                out.append({"node_id": nid, "error": exc.message})
        return {"nights": out, "count": len(out)}

    @server.tool()
    def tonight_accept(node_id: str, research_hours: float | None = None,
                       imaging_after: bool | None = None, note: str = "") -> dict:
        """Confirm tonight's run, optionally reshaping it.

        `research_hours` sets how long the research block lasts before imaging
        takes over. Accepting is not required — the recommendation runs anyway
        if nobody answers by the deadline — but it takes effect immediately
        rather than at dusk.
        """
        body: dict = {"decision": "accept"}
        if research_hours is not None:
            body["research_hours"] = float(research_hours)
        if imaging_after is not None:
            body["imaging_after"] = bool(imaging_after)
        if note:
            body["note"] = note
        return _with_nudge(
            client.post(f"/me/nodes/{encode_path(node_id)}/tonight", body))

    @server.tool()
    def tonight_decline(node_id: str, note: str = "") -> dict:
        """Sit tonight out. Affects tonight only; tomorrow is asked again."""
        body: dict = {"decision": "decline"}
        if note:
            body["note"] = note
        return _with_nudge(
            client.post(f"/me/nodes/{encode_path(node_id)}/tonight", body))

    @server.tool()
    def scheduled_night(node_id: str, date: str) -> dict:
        """What a telescope is scheduled to do on a future date (YYYY-MM-DD).

        Same shape as `tonight`, but for planning ahead instead of just
        today. Proposes a night if none exists yet for that date. Weather
        isn't checked this far out, so the verdict is based only on any
        decision made for that night.
        """
        return _with_nudge(
            client.get(f"/me/nodes/{encode_path(node_id)}/nights/{encode_path(date)}"),
            when=f"on {date}")

    @server.tool()
    def scheduled_night_accept(node_id: str, date: str,
                               research_hours: float | None = None,
                               imaging_after: bool | None = None,
                               note: str = "") -> dict:
        """Accept a future night's proposal (date is YYYY-MM-DD), optionally reshaping it."""
        body: dict = {"decision": "accept"}
        if research_hours is not None:
            body["research_hours"] = float(research_hours)
        if imaging_after is not None:
            body["imaging_after"] = bool(imaging_after)
        if note:
            body["note"] = note
        return _with_nudge(client.post(
            f"/me/nodes/{encode_path(node_id)}/nights/{encode_path(date)}", body),
            when=f"on {date}")

    @server.tool()
    def scheduled_night_decline(node_id: str, date: str, note: str = "") -> dict:
        """Decline a future night (date is YYYY-MM-DD) in advance, e.g. for a trip.

        Affects only that night; every other night is asked about separately.
        """
        body: dict = {"decision": "decline"}
        if note:
            body["note"] = note
        return _with_nudge(client.post(
            f"/me/nodes/{encode_path(node_id)}/nights/{encode_path(date)}", body),
            when=f"on {date}")

    @server.tool()
    def stand_down(node_id: str, reason: str = "", nights: int = 0) -> dict:
        """Stop observing now — the immediate override.

        Use this when something is wrong with the telescope, the weather turned,
        or the member simply wants it to stop. It reaches the node in about a
        second rather than waiting for its next poll, and nothing re-starts the
        night afterwards.

        `nights` > 0 also keeps the telescope out for that many further nights,
        which is what a vacation used to mean.
        """
        body: dict = {"reason": reason}
        if nights:
            body["nights"] = int(nights)
        return _with_nudge(
            client.post(f"/me/nodes/{encode_path(node_id)}/stand-down", body))

    @server.tool()
    def resume(node_id: str) -> dict:
        """Put a telescope back into the rotation after a stand-down or vacation.

        Clears any multi-night parking and accepts tonight's recommendation, so
        a telescope stood down earlier in the evening can still observe tonight.
        """
        try:
            client.delete(f"/me/nodes/{encode_path(node_id)}/vacation")
        except ApiError as exc:
            if exc.status not in (400, 404):   # no active vacation is fine
                raise
        return _with_nudge(client.post(
            f"/me/nodes/{encode_path(node_id)}/tonight", {"decision": "accept"}))

    @server.tool()
    def stand_down_all(reason: str = "", confirm: bool = False) -> dict:
        """Stop every telescope on this account at once. Requires confirm=true.

        For when something is wrong across the board — bad weather moving in,
        or a release that needs backing out.
        """
        require_confirmation(confirm, "stand down every telescope on this account")
        nodes = client.get("/me/nodes").get("nodes", [])
        results = []
        for node in nodes:
            nid = node.get("node_id")
            try:
                client.post(f"/me/nodes/{encode_path(nid)}/stand-down",
                            {"reason": reason})
                results.append({"node_id": nid, "stood_down": True})
            except ApiError as exc:
                results.append({"node_id": nid, "stood_down": False,
                                "error": exc.message})
        return {"results": results,
                "stood_down": sum(1 for r in results if r["stood_down"])}
