"""Fleet-integrity tools — the reason this server exists.

These do not exist in the Flutter app. They exist because the bugs that keep
reaching production are found by someone operating the system and noticing
something is off, and that does not scale past one person's attention.

All read-only, so they are safe to run unattended against production.
"""

from __future__ import annotations

from ..client import ApiError, CloudClient, encode_path


def register(server, client: CloudClient) -> None:

    @server.tool()
    def fleet_integrity_check() -> dict:
        """Run every fleet-integrity check and report what is broken.

        Covers orphaned nodes (a node with observations but no owner), dangling
        memberships, missing credentials, stale vacation status, silently dead
        heartbeat threads, ghost registrations and duplicate links.

        A clean result is the standing assertion that none of the bug classes
        fixed in 319fded, 0c4bb87, 9421bbd, 2cbc6a1, 5d926a1 or 9280ba7 have
        come back. Requires an admin key.
        """
        return client.get("/admin/fleet-integrity", admin=True)

    @server.tool()
    def node_identity_snapshot(node_id: str) -> dict:
        """Capture a node's identity and history for before/after comparison.

        Take one of these before anything that churns a node's credentials, and
        another after. If node_id, registration time or observation count
        changed, the node was re-registered rather than repaired — which is
        exactly the orphaning failure that loses a member's history.
        """
        fleet = client.get("/network/fleet")
        node = next((n for n in fleet.get("fleet", [])
                     if n.get("node_id") == node_id), None)
        owned = None
        try:
            mine = client.get("/me/nodes").get("nodes", [])
            owned = next((n for n in mine if n.get("node_id") == node_id), None)
        except ApiError:
            pass  # not signed in, or not this member's node

        if node is None and owned is None:
            return {"found": False, "node_id": node_id,
                    "detail": "No node with that id is visible in the fleet."}

        source = owned or node or {}
        return {
            "found": True,
            "node_id": node_id,
            "telescope_model": source.get("telescope_model"),
            "status": source.get("status"),
            "online": source.get("online"),
            "first_heartbeat_at": source.get("first_heartbeat_at"),
            "claimed_at": source.get("claimed_at"),
            "last_heartbeat": source.get("last_heartbeat"),
            "owned_by_this_member": owned is not None,
        }

    @server.tool()
    def assert_identity_stable(node_id: str, before: dict) -> dict:
        """Compare a node against an earlier node_identity_snapshot.

        Pass the dict returned by node_identity_snapshot as `before`. Reports
        which identity fields changed. A changed first_heartbeat_at or a node
        that stopped being owned means identity was lost, not repaired.
        """
        after = node_identity_snapshot(node_id)
        if not after.get("found"):
            return {"stable": False, "node_id": node_id,
                    "detail": "Node is no longer visible in the fleet at all.",
                    "before": before, "after": after}

        watched = ("node_id", "first_heartbeat_at", "claimed_at",
                   "owned_by_this_member", "telescope_model")
        changed = {
            field: {"before": before.get(field), "after": after.get(field)}
            for field in watched
            if before.get(field) is not None and before.get(field) != after.get(field)
        }
        return {
            "stable": not changed,
            "node_id": node_id,
            "changed_fields": changed,
            "detail": ("Identity preserved." if not changed else
                       "Identity changed — the node was re-registered rather "
                       "than repaired, which orphans its observation history."),
            "after": after,
        }

    @server.tool()
    def node_incidents(node_id: str = "") -> dict:
        """Structured incidents (failed solves, mount faults, upload errors).

        Without node_id, returns this member's incidents across all telescopes.
        """
        if node_id:
            result = client.get("/me/incidents")
            items = [i for i in result.get("incidents", [])
                     if i.get("node_id") == node_id]
            return {"node_id": node_id, "incidents": items}
        return client.get("/me/incidents")

    @server.tool()
    def pipeline_status() -> dict:
        """Where the network's data currently sits between capture and AAVSO.

        Compares network throughput against the AAVSO files actually produced,
        so frames stuck part-way through the pipeline show up as a gap rather
        than as silence.
        """
        status = client.get("/network/status")
        try:
            aavso = client.get("/aavso-files")
        except ApiError as exc:
            aavso = {"error": exc.message}
        return {"network": status, "aavso_files": aavso}
