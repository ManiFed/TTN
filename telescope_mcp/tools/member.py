"""Member-facing tools: everything app/lib/api/api_client.dart can do.

One tool per method on ApiClient, hitting the same endpoint with the same
arguments, so the chat interface and the Flutter app stay behaviourally
identical. Where the app shows a screen, this returns the JSON behind it.
"""

from __future__ import annotations

from ..client import ApiError, CloudClient, encode_path
from ..guard import require_confirmation, untrusted


def register(server, client: CloudClient) -> None:

    # ── Auth ──────────────────────────────────────────────────────────────

    @server.tool()
    def sign_in() -> dict:
        """Sign in, or create an account, by opening a browser window.

        Returns a link for the member to open. They sign in or sign up there,
        in a real browser against the real cloud, and this conversation
        receives the session afterwards — so no password is ever typed into a
        tool call, a transcript, or a model's context.

        Give them the link, then call `sign_in_status` with the returned code
        once they say they have finished. The link is single-use and lasts ten
        minutes.
        """
        result = client.post("/auth/browser/start", {})
        return {
            "open_this": result.get("url"),
            "code": result.get("code"),
            "expires_at": result.get("expires_at"),
            "next_step": (
                "Ask them to open that link and sign in or create an account. "
                "When they say they are done, call sign_in_status with the code."
            ),
        }

    @server.tool()
    def sign_in_status(code: str) -> dict:
        """Finish a browser sign-in started by `sign_in`.

        Call after the member says they have signed in. On success the session
        is saved on this computer so it survives an MCP process restart; the
        token itself is never returned.

        `code` may be the raw code from `sign_in`, or the full auth-link URL —
        the code is extracted either way so a pasted link still binds.
        """
        raw = (code or "").strip()
        # Agents sometimes paste the whole open_this URL; bind must still work.
        if "code=" in raw:
            from urllib.parse import parse_qs, urlparse
            try:
                qs = parse_qs(urlparse(raw).query)
                extracted = (qs.get("code") or [None])[0]
                if extracted:
                    raw = extracted.strip()
            except Exception:
                pass
        if not raw:
            return {"signed_in": False, "status": "expired",
                    "detail": "No sign-in code was provided. Call sign_in for a fresh link.",
                    "next_step": "Call sign_in for a fresh link."}

        result = client.post("/auth/browser/poll", {"code": raw})
        status = result.get("status")
        if status == "approved":
            token = result.get("token")
            if not token:
                return {"signed_in": False,
                        "detail": "The link was approved but carried no session. "
                                  "Start again with sign_in."}
            client.set_token(token)
            # Refresh/verify the bound session against /me so a successful
            # browser Done cannot leave member tools still auth-required (#50).
            try:
                me = client.get("/me")
            except ApiError as exc:
                # Only a 401 proves the token is invalid. Timeouts / connection
                # errors / 5xx leave the one-time poll token already consumed,
                # so clearing here would force a full browser re-signin.
                if exc.unauthorized:
                    client.set_token(None)
                    return {"signed_in": False,
                            "detail": (
                                "The link completed but this chat could not bind the "
                                f"session ({exc.message}). Call sign_in again."
                            ),
                            "next_step": "Call sign_in for a fresh link."}
                return {"signed_in": False,
                        "detail": (
                            "The link completed and a session was saved, but "
                            f"verification against /me failed ({exc.message}). "
                            "Retry sign_in_status — do not start a new link."
                        ),
                        "next_step": "Retry sign_in_status with the same code, "
                                     "or call auth_status."}
            return {"signed_in": True,
                    "user_id": me.get("user_id") or result.get("user_id"),
                    "detail": "Signed in. Everything on this account is available now."}
        return {"signed_in": False, "status": status,
                "detail": result.get("detail", ""),
                "next_step": ("Wait a moment and call sign_in_status again."
                              if status == "pending" else
                              "Call sign_in for a fresh link.")}

    @server.tool()
    def auth_login(email: str, password: str) -> dict:
        """Sign in to an existing Telescope Net member account.

        Holds the session on this computer (user-only file) so it survives an
        MCP process restart, and never returns the token. Signing in here does
        not sign the member out of the app — sessions are per-device.

        There is no sign-up tool: creating an account needs a password, and a
        password typed into a chat is a password in a transcript. Someone
        without an account should create one in the Telescope Net app first.
        """
        result = client.post("/auth/login", {"email": email, "password": password})
        token = result.get("token")
        if not token:
            return {"signed_in": False, "error": "No token returned."}
        client.set_token(token)
        return {"signed_in": True, "user_id": result.get("user_id")}

    @server.tool()
    def auth_logout() -> dict:
        """Sign out, revoking only this session. Other signed-in devices are unaffected."""
        try:
            client.post("/auth/logout", {})
        finally:
            client.set_token(None)
        return {"signed_in": False}

    @server.tool()
    def auth_status() -> dict:
        """Whether this conversation currently holds a valid member session.

        Checks the cloud, not just RAM: a recycled MCP process that restored
        its saved session still reports signed-in, and a dead session says
        to sign in again rather than a silent authenticated: false.
        """
        if not client.authenticated:
            return {
                "authenticated": False,
                "cloud": client.base,
                "detail": (
                    "Not signed in. Call sign_in, then sign_in_status "
                    "(or auth_login)."
                ),
            }
        try:
            me = client.get("/me")
        except ApiError as exc:
            if exc.unauthorized:
                return {
                    "authenticated": False,
                    "cloud": client.base,
                    "detail": (
                        "This chat lost its session. Sign in again with "
                        "sign_in."
                    ),
                }
            raise
        return {
            "authenticated": True,
            "cloud": client.base,
            "user_id": me.get("user_id"),
            "detail": "Signed in.",
        }

    # ── Member profile and totals ─────────────────────────────────────────

    @server.tool()
    def member_me() -> dict:
        """The signed-in member's profile: name, email, join date, role."""
        return client.get("/me")

    @server.tool()
    def member_stats() -> dict:
        """Lifetime contribution totals: observations, nights, targets, AAVSO submissions."""
        return client.get("/me/stats")

    @server.tool()
    def member_timeline() -> dict:
        """Chronological feed of this member's contributions and milestones."""
        return client.get("/me/timeline")

    @server.tool()
    def member_highlights() -> dict:
        """Unread highlight cards — notable things the member's telescopes did."""
        return client.get("/me/highlights")

    @server.tool()
    def member_mark_highlight_read(highlight_id: int) -> dict:
        """Mark one highlight card as read."""
        return client.post(f"/me/highlights/{int(highlight_id)}/read", {})

    # ── Telescopes ────────────────────────────────────────────────────────

    @server.tool()
    def member_list_nodes() -> dict:
        """Every telescope linked to this account, with status and last-seen time.

        Status reflects the calendar, not just the last heartbeat — a node whose
        vacation window has closed reports active even if it has not checked in
        since (cloud/registry.py::effective_status).
        """
        return client.get("/me/nodes")

    @server.tool()
    def member_node_live(node_id: str) -> dict:
        """Live detail for one telescope: current phase, target, conditions, queue depth."""
        return client.get(f"/me/nodes/{encode_path(node_id)}/live")

    @server.tool()
    def member_attach_node(
        location_name: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
        telescope_model: str = "",
        telescope_display_name: str = "",
        portable: bool = False,
        existing_node_id: str = "",
        existing_api_key: str = "",
    ) -> dict:
        """Link a telescope to this account, returning its node_id and api_key.

        Pass existing_node_id/existing_api_key when this computer already
        registered a node, so it is claimed rather than duplicated — registering
        a second one orphans the first along with its observation history.
        """
        body: dict = {}
        if latitude is not None and longitude is not None:
            body["latitude"] = latitude
            body["longitude"] = longitude
        if location_name:
            body["location_name"] = location_name
        if telescope_model:
            body["telescope_model"] = telescope_model
        if telescope_display_name:
            body["telescope_display_name"] = telescope_display_name
        if portable:
            body["portable"] = True
        if existing_node_id:
            body["node_id"] = existing_node_id
        if existing_api_key:
            body["api_key"] = existing_api_key
        return client.post("/me/nodes/attach", body)

    @server.tool()
    def member_claim_node(node_id: str, api_key: str) -> dict:
        """Claim an already-registered node onto this account using its credentials."""
        return client.post(f"/me/nodes/{encode_path(node_id)}", {"api_key": api_key})

    @server.tool()
    def member_rename_node(node_id: str, display_name: str) -> dict:
        """Set the member's own display name for a telescope."""
        return client.put(f"/me/nodes/{encode_path(node_id)}",
                          {"display_name": display_name})

    @server.tool()
    def member_disconnect_node(node_id: str, confirm: bool = False) -> dict:
        """Disconnect a telescope from this account. Requires confirm=true.

        The node keeps its identity and history; it simply stops being linked
        to this member.
        """
        require_confirmation(confirm, f"disconnect node {node_id}")
        return client.delete(f"/me/nodes/{encode_path(node_id)}")

    # ── Sessions and vacation ─────────────────────────────────────────────

    @server.tool()
    def member_start_session(node_id: str, latitude: float, longitude: float,
                             city: str, site_name: str = "") -> dict:
        """Start tonight's observing session for a portable telescope at a location.

        Portable nodes stay asleep until this is called — a heartbeat alone will
        not wake one. Returns the sky quality (mpsas, bortle) at that site.
        """
        return client.post(f"/me/nodes/{encode_path(node_id)}/session", {
            "lat": latitude, "lon": longitude,
            "city": city, "site_name": site_name,
        })

    @server.tool()
    def member_end_session(node_id: str) -> dict:
        """End a portable telescope's session early, returning it to sleeping."""
        return client.delete(f"/me/nodes/{encode_path(node_id)}/session")

    @server.tool()
    def member_set_vacation(node_id: str, until_date: str, from_date: str = "") -> dict:
        """Schedule a telescope's vacation. Dates are 'YYYY-MM-DD'.

        from_date defaults to today. A telescope on vacation is excluded from
        planning and will never raise a 'missed last night' alert.
        """
        body: dict = {"until_date": until_date}
        if from_date:
            body["from_date"] = from_date
        return client.put(f"/me/nodes/{encode_path(node_id)}/vacation", body)

    @server.tool()
    def member_cancel_vacation(node_id: str) -> dict:
        """Cancel a telescope's scheduled or active vacation."""
        return client.delete(f"/me/nodes/{encode_path(node_id)}/vacation")

    # ── Observations and science ──────────────────────────────────────────

    @server.tool()
    def member_observations(days: int = 90, limit: int = 200) -> dict:
        """This member's photometric observations over the last `days`."""
        return client.get("/me/observations", {"days": days, "limit": limit})

    @server.tool()
    def member_nights(limit: int = 30) -> dict:
        """Per-night summaries across all of this member's telescopes, newest first."""
        return client.get("/me/nights", {"limit": limit})

    @server.tool()
    def member_contributions() -> dict:
        """Frames and measurements this member contributed to the network."""
        return client.get("/me/contributions")

    @server.tool()
    def member_discoveries() -> dict:
        """Open Aperture: discovery candidates this member's telescopes helped find."""
        return client.get("/me/discoveries")

    @server.tool()
    def member_incidents() -> dict:
        """Faults reported by this member's telescopes (failed solves, mount errors)."""
        return client.get("/me/incidents")

    # ── Notifications ─────────────────────────────────────────────────────

    @server.tool()
    def member_notifications(limit: int = 50) -> dict:
        """Notifications with the unread count."""
        return client.get("/me/notifications", {"limit": limit})

    @server.tool()
    def member_mark_notification_read(notification_id: int) -> dict:
        """Mark one notification as read."""
        return client.post(f"/me/notifications/{int(notification_id)}/read", {})

    @server.tool()
    def member_notification_prefs(email: bool | None = None,
                                  push: bool | None = None) -> dict:
        """Turn email and push notifications on or off."""
        body: dict = {}
        if email is not None:
            body["notification_email"] = email
        if push is not None:
            body["notification_push"] = push
        if not body:
            return {"error": "Nothing to change — pass email and/or push."}
        return client.put("/me/notifications/prefs", body)

    # ── Programs and help ─────────────────────────────────────────────────

    @server.tool()
    def suggest_science_program(title: str, description: str,
                                target_examples: str = "", notes: str = "") -> dict:
        """Propose a target list or observing campaign for the network to run."""
        body = {"title": title, "description": description}
        if target_examples:
            body["target_examples"] = target_examples
        if notes:
            body["notes"] = notes
        return client.post("/me/science-program-suggestions", body)

    @server.tool()
    def help_session() -> dict:
        """Support contact details, remaining help-chat quota, and chat history."""
        result = client.get("/me/help")
        return untrusted(result, "cloud /me/help (includes member-authored chat history)")

    @server.tool()
    def help_chat(message: str, node_id: str = "") -> dict:
        """Send one message to the built-in help assistant. Limited to 5/week.

        This is a separate assistant with its own node telemetry context; it can
        queue config.yaml patches for a node to apply. Prefer answering directly
        from the other tools where possible rather than spending the quota.
        """
        body: dict = {"message": message}
        if node_id:
            body["node_id"] = node_id
        return untrusted(client.post("/me/help/chat", body),
                         "cloud /me/help/chat (third-party model output)")

    # ── Account ───────────────────────────────────────────────────────────

    @server.tool()
    def member_delete_account(confirm: bool = False) -> dict:
        """Permanently delete this member account. Requires confirm=true.

        This removes the account and its sessions. Only call it when the member
        has said so themselves, in this conversation, unprompted.
        """
        require_confirmation(confirm, "delete this member account")
        return client.delete("/me", {"confirm": True})
