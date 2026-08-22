"""Network and science tools — the public and read-only side of the cloud API.

These back the Tonight tab, target detail, Live Fleet and Open Aperture screens.
Most need no authentication, so they answer questions before anyone signs in.
"""

from __future__ import annotations

from ..client import CloudClient, encode_path
from ..guard import untrusted


def register(server, client: CloudClient) -> None:

    # ── Tonight / planning ────────────────────────────────────────────────

    @server.tool()
    def network_targets() -> dict:
        """Active targets across the network, sorted by observing priority."""
        return client.get("/targets")

    @server.tool()
    def target_details(object_name: str) -> dict:
        """Catalogue detail for one object: coordinates, magnitude, type, period."""
        return untrusted(client.get(f"/objects/{encode_path(object_name)}"),
                         "cloud object catalogue")

    @server.tool()
    def target_light_curve(target_name: str, days: int = 90) -> dict:
        """Photometric light curve for one target over the last `days`.

        Points carry BJD_TDB timestamps, magnitude, uncertainty, a quality flag
        and whether each measurement has been accepted by AAVSO. Note that AAVSO
        submissions report HJD_UTC, converted from these BJD values.
        """
        return client.get(f"/lightcurves/{encode_path(target_name)}", {"days": days})

    @server.tool()
    def target_light_curve_consensus(target_name: str) -> dict:
        """Cross-node consensus light curve for one target — the network's agreed view."""
        return client.get(f"/lightcurves/{encode_path(target_name)}/consensus")

    @server.tool()
    def survey_light_curve(source_key: str) -> dict:
        """Light curve for one survey source by its source key."""
        return client.get(f"/survey/lightcurves/{encode_path(source_key)}")

    # ── Conditions ────────────────────────────────────────────────────────

    @server.tool()
    def weather(latitude: float, longitude: float) -> dict:
        """Astronomy weather forecast (cloud cover, seeing, transparency) for a location."""
        return client.get("/weather", {"lat": latitude, "lon": longitude})

    @server.tool()
    def sky_quality(latitude: float, longitude: float) -> dict:
        """Sky darkness at a location: mpsas and Bortle class."""
        return client.get("/sky-quality", {"lat": latitude, "lon": longitude})

    @server.tool()
    def light_pollution(latitude: float, longitude: float) -> dict:
        """Sky brightness at a location, with the measurement source."""
        return client.get("/light-pollution", {"lat": latitude, "lon": longitude})

    # ── Fleet ─────────────────────────────────────────────────────────────

    @server.tool()
    def network_status() -> dict:
        """Network-wide summary: node counts, data volume, recent throughput."""
        return client.get("/network/status")

    @server.tool()
    def network_fleet() -> dict:
        """Every node on the network with its public status."""
        return client.get("/network/fleet")

    @server.tool()
    def network_live_fleet() -> dict:
        """Live fleet: what each telescope is doing now, plus mid-night reflows
        and reflex confirmations as they happen."""
        return client.get("/network/live-fleet")

    @server.tool()
    def network_activity() -> dict:
        """Recent network activity feed."""
        return client.get("/network/activity")

    # ── Events ────────────────────────────────────────────────────────────

    @server.tool()
    def event_details(event_id: str) -> dict:
        """Detail for one transient/alert event the network is responding to."""
        return untrusted(client.get(f"/events/{encode_path(event_id)}"),
                         "cloud event feed (ingested from external alert brokers)")

    @server.tool()
    def event_coverage(event_id: str) -> dict:
        """Which nodes covered an event, and how completely."""
        return client.get(f"/events/{encode_path(event_id)}/coverage")

    # ── Catalogue and releases ────────────────────────────────────────────

    @server.tool()
    def list_telescope_specs() -> dict:
        """Supported telescope models and their optical specs, for the linking flow."""
        return client.get("/telescopes")

    @server.tool()
    def latest_version() -> dict:
        """Newest published node/app release and where to download it."""
        return client.get("/versions")

    # ── Data products ─────────────────────────────────────────────────────

    @server.tool()
    def aavso_files() -> dict:
        """AAVSO submission files the network has generated."""
        return client.get("/aavso-files")

    @server.tool()
    def mpc_files() -> dict:
        """Minor Planet Center submission files the network has generated."""
        return client.get("/mpc-files")
