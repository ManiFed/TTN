"""Node-agent tools: everything the desktop app does against localhost:5173.

These run on the computer physically attached to a telescope and wrap
src/dashboard.py. Anything that moves the mount, opens the arm or exposes the
camera is refused against production until the loop is proven, because there
is a real instrument on the other end and it can be pointed at the sun.
"""

from __future__ import annotations

from ..client import AgentClient
from ..guard import require_confirmation, require_non_production, untrusted


def register(server, agent: AgentClient) -> None:

    # ── Status and diagnosis ──────────────────────────────────────────────

    @server.tool()
    def node_status() -> dict:
        """Full status of the node on this computer.

        Telescope and camera connection, safety state and reason, whether
        photometry is running, queued frame count, and commissioning progress.
        """
        return agent.get("/api/status")

    @server.tool()
    def node_logs(lines: int = 200) -> dict:
        """Recent node agent log lines — the first thing to read when something broke."""
        return untrusted(agent.get("/api/logs/recent", {"lines": int(lines)}),
                         "node agent log file")

    @server.tool()
    def node_commissioning() -> dict:
        """Commissioning progress: the checks a new telescope must pass."""
        return agent.get("/api/commissioning")

    @server.tool()
    def node_restart_commissioning(confirm: bool = False) -> dict:
        """Restart commissioning from the beginning. Requires confirm=true."""
        require_confirmation(confirm, "restart commissioning")
        return agent.post("/api/commissioning/restart")

    @server.tool()
    def node_safety() -> dict:
        """Current safety verdict and why: sun position, weather, horizon mask."""
        return agent.get("/api/safety")

    @server.tool()
    def node_photometry() -> dict:
        """Photometry pipeline state and queue depth."""
        return agent.get("/api/photometry")

    @server.tool()
    def node_aavso() -> dict:
        """AAVSO export status on this node."""
        return agent.get("/api/aavso")

    @server.tool()
    def node_config() -> dict:
        """This node's parsed config.yaml.

        Secrets are the agent's to redact; never repeat credential-looking
        values from this back to anyone.
        """
        return agent.get("/api/config/parsed")

    @server.tool()
    def node_events(limit: int = 50) -> dict:
        """Recent events recorded by the node agent."""
        return untrusted(agent.get("/api/events", {"limit": int(limit)}),
                         "node agent event log")

    @server.tool()
    def node_history(limit: int = 50) -> dict:
        """Images this node has captured, newest first."""
        return agent.get("/api/history", {"limit": int(limit)})

    @server.tool()
    def node_fits_list() -> dict:
        """FITS frames on disk on this node."""
        return agent.get("/api/fits/list")

    # ── Cloud identity ────────────────────────────────────────────────────

    @server.tool()
    def node_identity() -> dict:
        """Whether this node is registered with the cloud, and as which node_id.

        Never returns the api_key. Use this before linking a telescope so an
        already-registered node is claimed rather than duplicated.
        """
        result = agent.get("/api/cloud/identity")
        if not isinstance(result, dict):
            return {"registered": False}
        return {
            "registered": bool(result.get("registered")),
            "node_id": result.get("node_id"),
            "has_api_key": bool(result.get("api_key")),
        }

    @server.tool()
    def node_cloud_state() -> dict:
        """This node's cloud connection state and pairing token, if pairing."""
        result = agent.get("/api/cloud")
        if isinstance(result, dict):
            result.pop("api_key", None)
        return result

    @server.tool()
    def node_install_credentials(node_id: str, api_key: str) -> dict:
        """Install cloud credentials on this node, from member_attach_node.

        Repairs a node whose credentials the cloud rejected without changing
        its node_id, so its observation history survives.
        """
        agent.post("/api/cloud/credentials", {"node_id": node_id, "api_key": api_key})
        return {"installed": True, "node_id": node_id}

    # ── Telescope discovery and connection ────────────────────────────────

    @server.tool()
    def node_discover_alpaca() -> dict:
        """Broadcast an ALPACA discovery request and list every telescope that answers."""
        return agent.post("/api/discover", timeout=20.0)

    @server.tool()
    def node_connect_alpaca(host: str, port: int, set_as_default: bool = False) -> dict:
        """Connect this node to the ALPACA telescope server at host:port."""
        agent.post("/api/connect", {"host": host, "port": int(port),
                                    "set_as_default": set_as_default}, timeout=20.0)
        return {"connected": True, "host": host, "port": int(port)}

    @server.tool()
    def node_disconnect() -> dict:
        """Disconnect this node from its telescope."""
        return agent.post("/api/disconnect")

    # ── Mount and camera — real motion, environment-gated ─────────────────

    @server.tool()
    def node_park() -> dict:
        """Park the telescope. The safe resting position."""
        require_non_production("park the telescope")
        return agent.post("/api/telescope/park", timeout=60.0)

    @server.tool()
    def node_unpark() -> dict:
        """Unpark the telescope so it can slew."""
        require_non_production("unpark the telescope")
        return agent.post("/api/telescope/unpark", timeout=60.0)

    @server.tool()
    def node_set_tracking(enabled: bool) -> dict:
        """Turn sidereal tracking on or off."""
        require_non_production("change telescope tracking")
        return agent.post("/api/telescope/tracking", {"enabled": bool(enabled)})

    @server.tool()
    def node_slew(ra_hours: float, dec_deg: float) -> dict:
        """Slew the telescope to given coordinates. RA in hours, Dec in degrees.

        This physically moves the mount. Check node_safety first — the horizon
        mask and sun-avoidance exist to stop a slew that would damage the
        instrument or the observer's eyes.
        """
        require_non_production("slew the telescope")
        return agent.post("/api/slew", {"ra": ra_hours, "dec": dec_deg}, timeout=120.0)

    @server.tool()
    def node_nudge(direction: str, arcsec: float = 60.0) -> dict:
        """Nudge the telescope a small amount. Direction: north|south|east|west."""
        require_non_production("nudge the telescope")
        return agent.post("/api/telescope/nudge",
                          {"direction": direction, "arcsec": arcsec})

    @server.tool()
    def node_expose(seconds: float, count: int = 1) -> dict:
        """Take an exposure with the node's camera."""
        require_non_production("expose the camera")
        return agent.post("/api/camera/expose",
                          {"seconds": seconds, "count": int(count)},
                          timeout=max(60.0, seconds * count + 30.0))

    @server.tool()
    def node_abort_exposure() -> dict:
        """Abort the exposure in progress. Always allowed — this stops activity."""
        return agent.post("/api/camera/abort")

    @server.tool()
    def node_arm_open() -> dict:
        """Open the enclosure arm."""
        require_non_production("open the enclosure")
        return agent.post("/api/arm/open", timeout=60.0)

    @server.tool()
    def node_arm_close() -> dict:
        """Close the enclosure arm. Always allowed — this is the safe direction."""
        return agent.post("/api/arm/close", timeout=60.0)

    @server.tool()
    def node_safety_reset() -> dict:
        """Clear a latched safety stop, after the cause has actually been fixed."""
        require_non_production("clear the safety latch")
        return agent.post("/api/safety/reset")

    # ── Routines ──────────────────────────────────────────────────────────

    @server.tool()
    def node_autofocus_start() -> dict:
        """Start an autofocus run."""
        require_non_production("run autofocus")
        return agent.post("/api/focus/auto")

    @server.tool()
    def node_autofocus_status() -> dict:
        """Progress of the autofocus run."""
        return agent.get("/api/focus/auto/status")

    @server.tool()
    def node_autofocus_cancel() -> dict:
        """Cancel the autofocus run."""
        return agent.delete("/api/focus/auto")

    @server.tool()
    def node_center_start(ra_hours: float, dec_deg: float) -> dict:
        """Plate-solve and centre the telescope on given coordinates."""
        require_non_production("centre the telescope")
        return agent.post("/api/center/run", {"ra": ra_hours, "dec": dec_deg},
                          timeout=120.0)

    @server.tool()
    def node_center_status() -> dict:
        """Progress of the centring run."""
        return agent.get("/api/center/status")

    @server.tool()
    def node_center_cancel() -> dict:
        """Cancel the centring run."""
        return agent.delete("/api/center/run")

    @server.tool()
    def node_horizon_mask() -> dict:
        """The node's horizon mask — the altitude profile it refuses to slew below."""
        return agent.get("/api/safety/horizon-mask")

    @server.tool()
    def node_horizon_scan_start() -> dict:
        """Scan the local horizon to build a mask. Moves the mount through a survey."""
        require_non_production("run a horizon scan")
        return agent.post("/api/safety/horizon-scan")

    @server.tool()
    def node_horizon_scan_status() -> dict:
        """Progress of the horizon scan."""
        return agent.get("/api/safety/horizon-scan/status")

    @server.tool()
    def node_horizon_scan_cancel() -> dict:
        """Cancel the horizon scan."""
        return agent.delete("/api/safety/horizon-scan")

    @server.tool()
    def node_schedule_run() -> dict:
        """Run tonight's observing schedule now."""
        require_non_production("run the observing schedule")
        return agent.post("/api/schedule/run", timeout=60.0)

    @server.tool()
    def node_schedule_status() -> dict:
        """Progress through tonight's schedule."""
        return agent.get("/api/schedule/status")

    @server.tool()
    def node_schedule_abort() -> dict:
        """Abort the running schedule. Always allowed — this stops activity."""
        return agent.delete("/api/schedule/abort")
