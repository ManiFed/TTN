"""Images: what the telescope actually saw.

The rest of the surface returns numbers. This returns pictures, because at the
end of a night that is what a member wants to see — and because a photometry
network that never shows anyone their own sky is a harder thing to volunteer
for than it needs to be.

Images come back as real image content, not links, so they render in the
conversation rather than needing a download.
"""

from __future__ import annotations

from mcp.server.mcpserver.utilities.types import Image

from ..client import AgentClient, ApiError
from ..guard import require_non_production


def register(server, agent: AgentClient) -> None:

    @server.tool()
    def last_image() -> Image:
        """The most recent frame this telescope captured."""
        data, mime = agent.get_bytes("/api/image")
        return Image(data=data, format=_fmt(mime))

    @server.tool()
    def stacked_preview() -> Image:
        """The live-stacked image built up over tonight's exposures.

        This is the one worth looking at: single frames are noisy, the stack is
        what the night actually produced.
        """
        data, mime = agent.get_bytes("/api/stack/preview")
        return Image(data=data, format=_fmt(mime))

    @server.tool()
    def imaging_status() -> dict:
        """Whether the telescope is imaging right now, and what it is on.

        After a bounded research block the node picks an imaging target and
        starts stacking on its own, so this answers "what did it choose"
        without anyone having to be awake for the handover.
        """
        return agent.get("/api/imaging/status")

    @server.tool()
    def stack_status() -> dict:
        """How the live stack is progressing: frames in, frames kept, alignment."""
        return agent.get("/api/stack/status")

    @server.tool()
    def start_stacking() -> dict:
        """Begin live-stacking incoming frames into a single deeper image."""
        return agent.post("/api/stack/start")

    @server.tool()
    def stop_stacking() -> dict:
        """Stop live-stacking. The stack built so far is kept."""
        return agent.delete("/api/stack/start")

    @server.tool()
    def imaging_targets(search: str = "", limit: int = 20,
                        reachable_now: bool = False) -> dict:
        """Objects worth imaging, best first, from the node's own catalogue.

        Ranked the same way the automatic handover picks: recognisable Messier
        objects and nebulae ahead of the thousands of anonymous galaxies, so a
        suggestion here is something the telescope would actually choose.

        `reachable_now` filters to what is above the horizon mask and safe to
        slew to at this moment, which is slower but answers "what can I image
        tonight" rather than "what exists".
        """
        params = {"limit": max(1, int(limit))}
        if search:
            params["search"] = search
        if reachable_now:
            params["reachable"] = "1"
        return agent.get("/api/imaging/targets", params, timeout=30.0)

    @server.tool()
    def run_imaging_program(target_name: str, ra_hours: float, dec_deg: float,
                            exposure_s: float = 30.0) -> dict:
        """Point at a target and start building a stacked image of it.

        The imaging half of a night: slew, then live-stack incoming frames into
        one deeper image. Check `stack_status` as it builds and
        `stacked_preview` to look at it.

        This moves the mount, so it is refused against production unless
        explicitly allowed. Run it after the research block, not instead of it.
        """
        require_non_production(f"slew to {target_name} and start imaging")
        steps = []

        try:
            agent.post("/api/slew", {"ra": ra_hours, "dec": dec_deg}, timeout=120.0)
            steps.append({"step": "slew", "ok": True,
                          "detail": f"Pointed at {target_name}."})
        except ApiError as exc:
            return {"started": False, "steps": steps + [
                {"step": "slew", "ok": False, "detail": exc.message}],
                "detail": "Could not point the telescope. Check node_safety — "
                          "the horizon mask may be refusing this target."}

        try:
            agent.post("/api/center/run", {"ra": ra_hours, "dec": dec_deg},
                       timeout=120.0)
            steps.append({"step": "centre", "ok": True,
                          "detail": "Plate-solved and centred."})
        except ApiError as exc:
            # Centring is an improvement, not a requirement -- an uncentred
            # frame is still a frame.
            steps.append({"step": "centre", "ok": False, "detail": exc.message})

        try:
            agent.post("/api/stack/start", {"exposure_s": float(exposure_s)})
            steps.append({"step": "stack", "ok": True,
                          "detail": "Live stacking started."})
        except ApiError as exc:
            return {"started": False, "steps": steps + [
                {"step": "stack", "ok": False, "detail": exc.message}],
                "detail": "Pointed at the target but could not start stacking."}

        return {"started": True, "target": target_name, "steps": steps,
                "detail": (f"Imaging {target_name}. The stack deepens as frames "
                           f"come in — ask for the stacked preview any time, and "
                           f"again at the end of the night.")}

    @server.tool()
    def image_history(limit: int = 24) -> dict:
        """Images captured on this node, newest first, with their metadata."""
        return agent.get("/api/history", {"limit": int(limit)})

    @server.tool()
    def tonight_results() -> dict:
        """What tonight produced: what ran, what was measured, what was imaged.

        Assembled from the node's own view, so it works whether or not the
        cloud has finished ingesting the night. Use `stacked_preview` or
        `last_image` afterwards to actually look at it.
        """
        report: dict = {}
        for name, fetch in (
            ("schedule", lambda: agent.get("/api/schedule/status")),
            ("photometry", lambda: agent.get("/api/photometry")),
            ("aavso", lambda: agent.get("/api/aavso")),
            ("stack", lambda: agent.get("/api/stack/status")),
        ):
            try:
                report[name] = fetch()
            except ApiError as exc:
                report[name] = {"error": exc.message}

        try:
            report["images"] = agent.get("/api/history", {"limit": 24})
        except ApiError as exc:
            report["images"] = {"error": exc.message}

        photometry = report.get("photometry") or {}
        measured = photometry.get("measured") or photometry.get("count") or 0
        queued = photometry.get("queued") or 0
        report["summary"] = (
            f"{measured} measurement(s) produced tonight"
            + (f", {queued} frame(s) still in the queue" if queued else "")
            + ". Ask for the stacked preview to see the image."
        )
        return report


def _fmt(mime: str) -> str:
    """Image() wants a bare format name, not a MIME type."""
    return (mime or "image/png").rsplit("/", 1)[-1].split(";")[0].strip() or "png"
