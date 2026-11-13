#!/usr/bin/env python3
"""Issue #149: push site + UTC to the mount; read-only clock offset check.

Starfront 2026-11-12/13: the Seestar reported ``tracking: true`` but stars
trailed ~9 px/s at both 25 deg and 67 deg altitude (~2x the no-tracking drift).
TTN never sent SiteLatitude/SiteLongitude/SiteElevation/UTCDate, and the
Seestar loses its clock on power-off (SwitchBot cycles).
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import src.dashboard as dash
from alpaca.client import AlpacaClient, AlpacaError, NOT_IMPLEMENTED
from alpaca.telescope import (
    Telescope,
    clock_offset_seconds,
    format_utc_iso,
    parse_utc_iso,
)

UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _telescope_with_mock_client(get_values=None, put_side_effect=None):
    tel = Telescope.__new__(Telescope)
    client = MagicMock(spec=AlpacaClient)
    values = {
        "utcdate": "2026-11-13T03:00:00.000Z",
        "sitelatitude": 31.3614,
        "sitelongitude": -99.4431,
        "siteelevation": 525.0,
    }
    values.update(get_values or {})

    def _get(attr, **_kw):
        v = values[attr]
        if isinstance(v, Exception):
            raise v
        return v

    client._get.side_effect = _get
    if put_side_effect is not None:
        client._put.side_effect = put_side_effect
    tel._c = client
    return tel, client


class UtcFormatTest(unittest.TestCase):
    def test_iso_ms_z(self):
        dt = datetime(2026, 11, 13, 3, 4, 5, 678901, tzinfo=timezone.utc)
        self.assertEqual(format_utc_iso(dt), "2026-11-13T03:04:05.678Z")

    def test_naive_treated_as_utc_and_offsets_converted(self):
        self.assertEqual(format_utc_iso(datetime(2026, 1, 2, 3, 4, 5)),
                         "2026-01-02T03:04:05.000Z")
        ct = timezone(timedelta(hours=-6))
        self.assertEqual(format_utc_iso(datetime(2026, 11, 12, 21, 0, 0, tzinfo=ct)),
                         "2026-11-13T03:00:00.000Z")

    def test_default_is_now(self):
        self.assertRegex(format_utc_iso(), UTC_RE)

    def test_parse_variants(self):
        ref = datetime(2026, 11, 13, 3, 0, 0, 500000, tzinfo=timezone.utc)
        for s in ("2026-11-13T03:00:00.5Z", "2026-11-13T03:00:00.5000000Z",
                  "2026-11-13T03:00:00.500+00:00", "2026-11-13T03:00:00.500",
                  "2026-11-12T21:00:00.500-06:00"):
            self.assertEqual(parse_utc_iso(s), ref, s)
        self.assertIsNone(parse_utc_iso("garbage"))
        self.assertIsNone(parse_utc_iso(None))


class ClockOffsetTest(unittest.TestCase):
    def test_offset_sign_and_value(self):
        now = datetime(2026, 11, 13, 3, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(clock_offset_seconds("2026-11-13T03:00:10.250Z", now), 10.25)
        self.assertEqual(clock_offset_seconds("2026-11-13T02:59:00.000Z", now), -60.0)
        # Seestar clock reset to an epoch after a power cycle -> huge negative.
        self.assertLess(clock_offset_seconds("1970-01-01T00:00:00Z", now), -1e9)
        self.assertIsNone(clock_offset_seconds("not a date", now))

    def test_read_site_and_time_reports_offset(self):
        dev = format_utc_iso(datetime.now(timezone.utc) - timedelta(hours=1))
        tel, client = _telescope_with_mock_client({"utcdate": dev})
        out = tel.read_site_and_time()
        self.assertAlmostEqual(out["clock_offset_s"], -3600.0, delta=5.0)
        self.assertEqual(out["sitelatitude"], 31.3614)
        self.assertEqual(out["sitelongitude"], -99.4431)
        self.assertEqual(out["siteelevation"], 525.0)
        client._put.assert_not_called()  # read-only

    def test_offset_not_inflated_by_slow_site_reads(self):
        """Codex review on #150: node UTC is sampled at the utcdate GET."""
        import alpaca.telescope as tmod
        t0 = datetime(2026, 11, 13, 3, 0, 0, tzinfo=timezone.utc)
        clock = {"t": t0}

        class _FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["t"]

        tel, client = _telescope_with_mock_client({"utcdate": "2026-11-13T03:00:00.000Z"})
        base_get = client._get.side_effect

        def _slow_get(attr, **kw):
            v = base_get(attr, **kw)
            if attr != "utcdate":
                clock["t"] = clock["t"] + timedelta(seconds=10)  # each read "times out"
            return v

        client._get.side_effect = _slow_get
        with patch.object(tmod, "datetime", _FakeDT):
            out = tel.read_site_and_time()
        self.assertEqual(out["clock_offset_s"], 0.0)
        self.assertEqual(out["node_utc"], "2026-11-13T03:00:00.000Z")

    def test_read_tolerates_missing_properties(self):
        tel, _ = _telescope_with_mock_client(
            {"siteelevation": AlpacaError("not implemented", code=NOT_IMPLEMENTED)})
        out = tel.read_site_and_time()
        self.assertIsNone(out["siteelevation"])
        self.assertIn("siteelevation", out["errors"])


class SyncPayloadTest(unittest.TestCase):
    def test_put_payloads(self):
        tel, client = _telescope_with_mock_client()
        now = datetime(2026, 11, 13, 3, 4, 5, 678000, tzinfo=timezone.utc)
        out = tel.sync_site_and_time(31.3614, -99.4431, 525, now=now)
        calls = [(c.args[0], c.kwargs) for c in client._put.call_args_list]
        self.assertEqual(calls, [
            ("sitelatitude", {"SiteLatitude": 31.3614}),
            ("sitelongitude", {"SiteLongitude": -99.4431}),  # east-positive: TX < 0
            ("siteelevation", {"SiteElevation": 525.0}),
            ("utcdate", {"UTCDate": "2026-11-13T03:04:05.678Z"}),
        ])
        self.assertTrue(out["ok"])
        self.assertEqual(out["put_errors"], {})
        self.assertEqual(out["sitelatitude"], 31.3614)  # read back
        self.assertIn("clock_offset_s", out)

    def test_elevation_omitted_when_unknown(self):
        tel, client = _telescope_with_mock_client()
        tel.sync_site_and_time(31.0, -99.0, None)
        self.assertNotIn("siteelevation", [c.args[0] for c in client._put.call_args_list])

    def test_utcdate_format_default_now(self):
        tel, client = _telescope_with_mock_client()
        tel.sync_site_and_time(31.0, -99.0, 0)
        utc = [c.kwargs["UTCDate"] for c in client._put.call_args_list
               if c.args[0] == "utcdate"][0]
        self.assertRegex(utc, UTC_RE)


class SyncFailSoftTest(unittest.TestCase):
    def test_elevation_not_settable_is_tolerated(self):
        def _put(action, **kw):
            if action == "siteelevation":
                raise AlpacaError("SiteElevation not implemented", code=NOT_IMPLEMENTED)
        tel, client = _telescope_with_mock_client(put_side_effect=_put)
        with self.assertLogs("alpaca.telescope", level="INFO") as logs:
            out = tel.sync_site_and_time(31.0, -99.0, 500)
        self.assertTrue(out["ok"])  # elevation is not critical
        self.assertIn("siteelevation", out["put_errors"])
        self.assertTrue(any("not settable" in m for m in logs.output))
        self.assertIn("utcdate", [c.args[0] for c in client._put.call_args_list])

    def test_every_put_failing_never_raises(self):
        def _put(action, **kw):
            raise AlpacaError(f"{action} refused", code=0x500)
        tel, client = _telescope_with_mock_client(put_side_effect=_put)
        with self.assertLogs("alpaca.telescope", level="WARNING") as logs:
            out = tel.sync_site_and_time(31.0, -99.0, 500)
        self.assertFalse(out["ok"])
        self.assertEqual(set(out["put_errors"]),
                         {"sitelatitude", "sitelongitude", "siteelevation", "utcdate"})
        self.assertEqual(client._put.call_count, 4)  # kept going after failures
        self.assertTrue(any("sync failed" in m for m in logs.output))

    def test_transport_errors_fail_soft(self):
        def _put(action, **kw):
            raise ConnectionError("unreachable")
        tel, _ = _telescope_with_mock_client(
            {"utcdate": ConnectionError("unreachable")}, put_side_effect=_put)
        out = tel.sync_site_and_time(31.0, -99.0, None)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["clock_offset_s"])


class DashboardWiringTest(unittest.TestCase):
    """The sync runs on connect, schedule prep, slews and tracking-on."""

    def setUp(self):
        self.client = dash.app.test_client()
        self.tel = MagicMock(spec=Telescope)
        self.tel.sync_site_and_time.return_value = {"ok": True, "clock_offset_s": 0.1}
        self.tel.is_parked.return_value = False
        self.tel.is_tracking.return_value = False
        self._site = patch.object(dash, "_observer_site_from_disk",
                                  return_value=(31.3614, -99.4431, 525.0))
        self._site.start()

    def tearDown(self):
        self._site.stop()
        with dash._state_lock:
            dash._state["telescope"]["site_time"] = None

    def test_helper_uses_config_site_and_records_state(self):
        with patch.object(dash, "_tel", self.tel):
            res = dash._sync_mount_site_time("unit")
        self.tel.sync_site_and_time.assert_called_once_with(31.3614, -99.4431, 525.0)
        self.assertEqual(res["reason"], "unit")
        self.assertEqual(dash._state["telescope"]["site_time"]["reason"], "unit")

    def test_helper_never_raises(self):
        self.tel.sync_site_and_time.side_effect = RuntimeError("boom")
        with patch.object(dash, "_tel", self.tel):
            self.assertIsNone(dash._sync_mount_site_time("unit"))

    def test_helper_skips_without_location(self):
        with patch.object(dash, "_tel", self.tel), \
             patch.object(dash, "_observer_site_from_disk", return_value=(0.0, 0.0, None)):
            self.assertIsNone(dash._sync_mount_site_time("unit"))
        self.tel.sync_site_and_time.assert_not_called()

    def test_sync_on_connect(self):
        cfg = {"alpaca": {"api_version": 1},
               "devices": {"telescope": {"enabled": True, "device_number": 0}}}
        with patch.object(dash, "_load_config", return_value=cfg), \
             patch.object(dash, "Telescope", return_value=self.tel), \
             patch.object(dash, "_safety_mgr", None), \
             patch("alpaca.discovery._fetch_device_info", return_value={}), \
             patch.object(dash, "_tel", None), patch.object(dash, "_cam", None), \
             patch.object(dash, "_foc", None), patch.object(dash, "_cover", None):
            try:
                dash._do_connect("172.22.6.32", 32323)
            except Exception:
                pass  # later device phases are irrelevant here
        self.tel.connect.assert_called_once()
        self.tel.sync_site_and_time.assert_called_once_with(31.3614, -99.4431, 525.0)

    def test_sync_before_eq_slew(self):
        order = []
        self.tel.sync_site_and_time.side_effect = lambda *a: order.append("sync") or {}
        self.tel.begin_slew.side_effect = lambda *a: order.append("slew")
        with patch.object(dash, "_tel", self.tel), \
             patch.object(dash, "_science_capture_active", return_value=False), \
             patch.object(dash, "_stand_down_latched", return_value=False), \
             patch.object(dash, "_slew_rejection", return_value=None), \
             patch.object(dash, "_remember_commanded_slew"):
            r = self.client.post("/api/slew", json={"ra": 21.7, "dec": 43.6})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(order, ["sync", "slew"])

    def test_sync_before_altaz_slew(self):
        order = []
        self.tel.sync_site_and_time.side_effect = lambda *a: order.append("sync") or {}
        self.tel.begin_slew_altaz.side_effect = lambda *a: order.append("slew")
        with patch.object(dash, "_tel", self.tel), \
             patch.object(dash, "_science_capture_active", return_value=False), \
             patch.object(dash, "_stand_down_latched", return_value=False), \
             patch.object(dash, "_safety_mgr", None):
            r = self.client.post("/api/slew", json={"mode": "altaz", "alt": 60, "az": 90})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(order, ["sync", "slew"])

    def test_slew_proceeds_when_sync_fails(self):
        self.tel.sync_site_and_time.side_effect = AlpacaError("nope", code=1024)
        with patch.object(dash, "_tel", self.tel), \
             patch.object(dash, "_science_capture_active", return_value=False), \
             patch.object(dash, "_stand_down_latched", return_value=False), \
             patch.object(dash, "_slew_rejection", return_value=None), \
             patch.object(dash, "_remember_commanded_slew"):
            r = self.client.post("/api/slew", json={"ra": 21.7, "dec": 43.6})
        self.assertEqual(r.status_code, 200)
        self.tel.begin_slew.assert_called_once()

    def test_sync_after_tracking_on_only(self):
        with patch.object(dash, "_tel", self.tel):
            self.client.post("/api/telescope/tracking", json={"enabled": False})
            self.tel.sync_site_and_time.assert_not_called()
            self.client.post("/api/telescope/tracking", json={"enabled": True})
        self.tel.set_tracking.assert_called_with(True)
        self.tel.sync_site_and_time.assert_called_once()

    def test_schedule_prep_syncs_first(self):
        order = []
        self.tel.sync_site_and_time.side_effect = lambda *a: order.append("sync") or {}
        self.tel.set_tracking.side_effect = lambda *a: order.append("track")
        with patch.object(dash, "_tel", self.tel), patch.object(dash, "_safety_mgr", None):
            dash._sched_prepare_mount()
        self.assertEqual(order, ["sync", "track", "sync"])

    def test_site_time_route_is_read_only(self):
        self.tel.read_site_and_time.return_value = {
            "utcdate": "2026-11-13T03:00:00.000Z", "sitelatitude": 31.36,
            "sitelongitude": -99.44, "siteelevation": 0.0, "clock_offset_s": -12.5,
            "errors": {}}
        with patch.object(dash, "_tel", self.tel):
            r = self.client.get("/api/telescope/site-time")
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["clock_offset_s"], -12.5)
        self.assertEqual(body["configured"]["latitude"], 31.3614)
        self.tel.sync_site_and_time.assert_not_called()

    def test_routes_never_return_exception_text(self):
        secret = "Traceback ... /Users/someone/secret/path"
        self.tel.read_site_and_time.return_value = {
            "utcdate": None, "sitelatitude": 31.36, "sitelongitude": -99.44,
            "siteelevation": None, "clock_offset_s": None,
            "errors": {"utcdate": secret, "siteelevation": secret}}
        self.tel.sync_site_and_time.return_value = {
            "ok": False, "sent": {"utcdate": "2026-11-13T03:00:00.000Z"},
            "put_errors": {"utcdate": secret}, "errors": {"utcdate": secret}}
        with patch.object(dash, "_tel", self.tel):
            r1 = self.client.get("/api/telescope/site-time")
            r2 = self.client.post("/api/telescope/site-time/sync")
            r3 = self.client.get("/api/status")
        for r in (r1, r2, r3):
            self.assertNotIn("secret/path", r.get_data(as_text=True))
        self.assertEqual(r1.get_json()["read_failed"], ["siteelevation", "utcdate"])
        self.assertEqual(r2.get_json()["put_failed"], ["utcdate"])
        self.assertFalse(r2.get_json()["ok"])

    def test_manual_sync_route(self):
        with patch.object(dash, "_tel", self.tel):
            r = self.client.post("/api/telescope/site-time/sync")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["reason"], "manual")


class McpToolsTest(unittest.TestCase):
    def test_tools_registered_and_routed(self):
        from telescope_mcp.client import AgentClient
        from telescope_mcp.local_server import build_server
        from tests.test_mcp_guards import call
        agent = MagicMock(spec=AgentClient)
        agent.get.return_value = {"clock_offset_s": 0.0}
        agent.post.return_value = {"ok": True}
        server = build_server(agent, with_cloud=False)
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "production"}, clear=True):
            ok, _ = call(server, "node_mount_site_time", {})
            self.assertTrue(ok)
            agent.get.assert_called_with("/api/telescope/site-time")
            ok, _ = call(server, "node_mount_sync_site_time", {})
            self.assertTrue(ok)
        self.assertEqual(agent.post.call_args.args[0], "/api/telescope/site-time/sync")


if __name__ == "__main__":
    unittest.main()
