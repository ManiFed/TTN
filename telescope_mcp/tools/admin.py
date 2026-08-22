"""Admin tools — fleet operations that need an admin account or admin key.

Two auth tiers exist in the cloud and are preserved here: dry-run runs on a
member session whose account has role='admin' (auth.require_admin_member),
while the fleet-wide operations use the static X-Admin-Key
(TELESCOPE_MCP_ADMIN_KEY). Tools that trigger real observing work are gated
on the target environment.
"""

from __future__ import annotations

from ..client import CloudClient, encode_path
from ..guard import require_confirmation, require_non_production, untrusted


def register(server, client: CloudClient) -> None:

    # ── Dry-run testing (member session, role='admin') ────────────────────

    @server.tool()
    def admin_start_dry_run(node_id: str, minutes: int = 240) -> dict:
        """Force a telescope through a full night run in daylight, for testing.

        Bypasses the real night-window gate and the node's own sun-position
        safety check, so the mount will actually slew and the camera will
        actually expose. This moves real hardware — refused against production
        unless explicitly allowed.
        """
        require_non_production(f"start a dry run on node {node_id}")
        return client.put(f"/admin/nodes/{encode_path(node_id)}/dry-run",
                          {"minutes": int(minutes)})

    @server.tool()
    def admin_stop_dry_run(node_id: str) -> dict:
        """Turn off dry-run testing mode on a telescope immediately."""
        return client.delete(f"/admin/nodes/{encode_path(node_id)}/dry-run")

    # ── Scheduling and planning ───────────────────────────────────────────

    @server.tool()
    def admin_replan() -> dict:
        """Rescore every target and regenerate all node plans."""
        require_non_production("regenerate every node's observing plan")
        return client.post("/admin/replan", {}, admin=True)

    @server.tool()
    def admin_ingest_alerts() -> dict:
        """Run transient-alert ingestion now instead of waiting for the schedule."""
        require_non_production("run alert ingestion")
        return client.post("/admin/ingest", {}, admin=True)

    @server.tool()
    def admin_broadcast_interrupt(name: str, ra_deg: float, dec_deg: float,
                                  reason: str, mag: float | None = None,
                                  confirm: bool = False) -> dict:
        """Broadcast a high-priority target that interrupts every node's plan.

        This redirects the whole fleet. Requires confirm=true.
        """
        require_non_production("interrupt every node on the network")
        require_confirmation(confirm, f"broadcast interrupt '{name}' to the fleet")
        body = {"name": name, "ra_deg": ra_deg, "dec_deg": dec_deg, "reason": reason}
        if mag is not None:
            body["mag"] = mag
        return client.post("/interrupts", body, admin=True)

    @server.tool()
    def admin_list_events() -> dict:
        """Alert events the network is currently responding to."""
        return untrusted(client.get("/admin/events", admin=True),
                         "cloud event feed (ingested from external alert brokers)")

    @server.tool()
    def admin_cancel_event(event_id: str, confirm: bool = False) -> dict:
        """Stop the network responding to one alert event. Requires confirm=true."""
        require_confirmation(confirm, f"cancel event {event_id}")
        return client.post(f"/admin/events/{encode_path(event_id)}/cancel", {}, admin=True)

    # ── Scoring and calibration ───────────────────────────────────────────

    @server.tool()
    def admin_tuning() -> dict:
        """Active target-scoring weights and their tuning history."""
        return client.get("/admin/tuning", admin=True)

    @server.tool()
    def admin_rollback_tuning(confirm: bool = False) -> dict:
        """Restore the previous scoring weights. Requires confirm=true."""
        require_confirmation(confirm, "roll back the network's scoring weights")
        return client.post("/admin/tuning/rollback", {}, admin=True)

    @server.tool()
    def admin_calibration_models() -> dict:
        """Photometric calibration models and their versions."""
        return client.get("/admin/calibration/models", admin=True)

    @server.tool()
    def admin_rollback_calibration(model_version: str, confirm: bool = False) -> dict:
        """Roll back to a previous calibration model version. Requires confirm=true.

        Calibration feeds the magnitudes submitted to AAVSO under the network's
        obscode, so a wrong model version corrupts published science.
        """
        require_confirmation(confirm, f"roll back calibration to {model_version}")
        return client.post(
            f"/admin/calibration/models/{encode_path(model_version)}/rollback",
            {}, admin=True)

    # ── AAVSO ─────────────────────────────────────────────────────────────

    @server.tool()
    def admin_aavso_batches() -> dict:
        """Recent AAVSO submission batches and their status.

        Submissions report HJD_UTC, converted from the BJD_TDB stored with each
        measurement — the two differ by roughly 68 seconds.
        """
        return client.get("/admin/aavso-batches", admin=True)

    @server.tool()
    def admin_mark_aavso_submitted(batch_id: int, confirm: bool = False) -> dict:
        """Mark an AAVSO batch as emailed to AAVSO. Requires confirm=true.

        This is a bookkeeping change only — it records that a human sent the
        batch. It does not submit anything.
        """
        require_confirmation(confirm, f"mark AAVSO batch {batch_id} as submitted")
        return client.post(f"/admin/aavso-batches/{int(batch_id)}/mark-submitted",
                           {}, admin=True)

    # ── Candidates and review queues ──────────────────────────────────────

    @server.tool()
    def admin_candidates() -> dict:
        """Discovery candidates awaiting review."""
        return client.get("/admin/candidates", admin=True)

    @server.tool()
    def admin_asteroid_candidates() -> dict:
        """Asteroid candidates awaiting review."""
        return client.get("/admin/asteroid-candidates", admin=True)

    @server.tool()
    def admin_update_candidate(candidate_id: int, status: str) -> dict:
        """Set the review status on a discovery candidate."""
        return client.patch(f"/admin/candidates/{int(candidate_id)}",
                            {"status": status}, admin=True)

    @server.tool()
    def admin_incidents(status: str = "active") -> dict:
        """Structured incidents across the fleet. status: active|open|resolved|all."""
        return client.get("/admin/incidents", {"status": status}, admin=True)

    @server.tool()
    def admin_science_suggestions() -> dict:
        """Member-submitted science program ideas awaiting triage."""
        return untrusted(client.get("/admin/science-program-suggestions", admin=True),
                         "member-authored suggestion text")

    @server.tool()
    def admin_sky_quality() -> dict:
        """Measured sky quality across the fleet."""
        return client.get("/admin/sky-quality", admin=True)

    @server.tool()
    def admin_patrol() -> dict:
        """Sky-patrol coverage status."""
        return client.get("/admin/patrol", admin=True)
