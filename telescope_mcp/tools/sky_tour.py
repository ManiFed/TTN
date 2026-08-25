"""A small, guided observing tour for the telescope at this computer.

The tour deliberately advances one stop at a time.  A guide should not queue
several physical slews while nobody is watching: the mount's own safety checks
remain authoritative, and the person can stop after any object.
"""

from __future__ import annotations

from ..client import AgentClient, ApiError
from ..guard import require_non_production


# Coordinates are J2000-ish, expressed in the mount API's RA-hours/Dec-degrees
# convention.  The local slew endpoint still applies the actual horizon and
# sun-avoidance checks for this particular telescope.
TOURS: dict[str, tuple[dict[str, object], ...]] = {
    "showcase": (
        {
            "id": "M42", "name": "Orion Nebula", "ra_hours": 5.5881,
            "dec_deg": -5.3911,
            "story": "A stellar nursery about 1,300 light-years away. Its glow comes from young, hot stars energising the surrounding gas.",
        },
        {
            "id": "M45", "name": "Pleiades", "ra_hours": 3.7922,
            "dec_deg": 24.1051,
            "story": "The Seven Sisters are a nearby open cluster, roughly 440 light-years away. The blue haze is dust reflecting the stars' light.",
        },
        {
            "id": "M13", "name": "Hercules Globular Cluster", "ra_hours": 16.6949,
            "dec_deg": 36.4613,
            "story": "Hundreds of thousands of ancient stars are packed into this globe, orbiting the Milky Way's halo about 22,000 light-years from us.",
        },
        {
            "id": "M57", "name": "Ring Nebula", "ra_hours": 18.8930,
            "dec_deg": 33.0292,
            "story": "This ring is the expanding outer atmosphere of a dying Sun-like star. The tiny central remnant is becoming a white dwarf.",
        },
    ),
}


def _tour_summary(tour: str) -> list[dict[str, object]]:
    return [
        {"stop": i + 1, "id": stop["id"], "name": stop["name"],
         "story": stop["story"]}
        for i, stop in enumerate(TOURS[tour])
    ]


def register(server, agent: AgentClient) -> None:
    """Register a guide whose progress lasts for this MCP-server session."""
    active_tour: str | None = None
    next_stop = 0

    @server.tool()
    def sky_tour(action: str = "preview", tour: str = "showcase") -> dict:
        """Guide a telescope around striking deep-sky objects, one stop at a time.

        Use `action="preview"` to see the four-stop tour without moving the
        telescope. `action="start"` moves to its first object; then call
        `action="next"` after discussing or viewing each object. `action="stop"`
        ends the tour without moving the mount. Each successful stop includes a
        short explanation and names the next one.

        The mount's local horizon and sun-avoidance checks are applied to every
        stop. This physically moves the telescope and is refused in production
        unless production hardware writes were explicitly enabled.
        """
        nonlocal active_tour, next_stop

        action = action.strip().lower()
        tour = tour.strip().lower()
        if action not in {"preview", "start", "next", "stop"}:
            return {"started": False, "detail": "action must be preview, start, next, or stop."}
        if tour not in TOURS:
            return {"started": False, "detail": f"Unknown tour '{tour}'.",
                    "tours": sorted(TOURS)}

        if action == "preview":
            return {"tour": tour, "stops": _tour_summary(tour),
                    "detail": "Preview only: no telescope motion. Start when you are ready."}
        if action == "stop":
            active_tour, next_stop = None, 0
            return {"stopped": True, "detail": "Sky tour ended. The telescope stays where it is."}

        if action == "start":
            require_non_production("start a guided sky tour")
            active_tour, next_stop = tour, 0
        elif active_tour is None:
            return {"started": False, "detail": "No sky tour is active. Preview or start one first."}
        elif tour != active_tour:
            return {"started": False, "detail": f"The active tour is '{active_tour}'. Continue it or stop it first."}
        else:
            require_non_production("advance a guided sky tour")

        stops = TOURS[active_tour]
        if next_stop >= len(stops):
            active_tour, next_stop = None, 0
            return {"complete": True, "detail": "The sky tour is complete."}

        stop = stops[next_stop]
        try:
            agent.post("/api/slew", {"ra": stop["ra_hours"], "dec": stop["dec_deg"]},
                       timeout=120.0)
        except ApiError as exc:
            return {"started": True, "moved": False, "stop": next_stop + 1,
                    "target": stop["name"], "detail": (
                        f"Could not reach {stop['name']}: {exc.message}. "
                        "The tour is still paused here; check node_safety or stop the tour.")}

        next_stop += 1
        following = stops[next_stop] if next_stop < len(stops) else None
        result = {
            "started": True, "moved": True, "tour": active_tour,
            "stop": next_stop, "stops_total": len(stops),
            "target": stop["name"], "catalog_id": stop["id"],
            "story": stop["story"],
        }
        if following:
            result["next"] = {"target": following["name"], "catalog_id": following["id"],
                              "detail": "Call sky_tour(action='next') when you are ready to continue."}
        else:
            result["next"] = {"detail": "This is the final stop. Call sky_tour(action='next') to close the tour."}
        return result
