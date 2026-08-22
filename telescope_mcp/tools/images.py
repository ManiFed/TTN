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
