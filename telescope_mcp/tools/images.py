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
    def imaging_targets(search: str = "", limit: int = 20) -> dict:
        """Deep-sky objects this telescope could image, from its own catalogue.

        Use after the research block to pick something worth looking at. The
        catalogue lives on the node, so this works without the cloud.
        """
        catalog = agent.get("/api/catalog", timeout=15.0)
        items = catalog if isinstance(catalog, list) else (
            catalog.get("objects") or catalog.get("catalog") or [])
        if search:
            needle = search.lower()
            items = [o for o in items
                     if needle in str(o.get("name", "")).lower()
                     or needle in str(o.get("common_name", "")).lower()]
        return {"targets": items[:max(1, int(limit))], "total": len(items)}

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
