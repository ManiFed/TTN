"""Gauntlet: CloudCommunicator vs. an unreliable cloud (F7, F10, F11).

Simulates outages, HTTP 500s, rejected registrations, crash-corrupted queue
files, and deep upload backlogs — asserting the node never loses data
silently, never hammers the API without backoff, and always leaves evidence.
"""

import json
import os
import pathlib
import unittest
from unittest.mock import patch

from src import telemetry
from src.cloud_communicator import CloudCommunicator, keyring
from tests.gauntlet.fakecloud import FakeCloud
from tests.gauntlet.util import TempCwdTestCase


def _config(url: str) -> dict:
    return {
        "cloud": {
            "enabled": True,
            "url": url,
            "node_id": "",
            "api_key": "",
            "heartbeat_interval": 1,
            "plan_poll_interval": 1,
        },
        "observatory": {"latitude": 31.36, "longitude": -99.44,
                        "elevation": 500, "observer": "Gauntlet"},
        "photometry": {"node_id": "", "filter_name": "CV"},
    }


class CloudCommGauntletTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        self.fake = FakeCloud().start()

    def tearDown(self):
        self.fake.stop()
        telemetry.reset_for_tests()
        super().tearDown()

    def _comm(self, url=None) -> CloudCommunicator:
        return CloudCommunicator(_config(url or self.fake.url))

    # ── Registration ───────────────────────────────────────────────────────────

    def test_registration_success_persists_credentials(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        state = json.loads(pathlib.Path("data", "cloud_state.json").read_text())
        self.assertEqual(state["node_id"], "node_test01")
        self.assertNotIn("api_key", state)
        # Credentials are restored from the system keyring, not a state file.
        self.fake.clear()
        comm2 = self._comm()
        self.assertTrue(comm2._ensure_registered())
        self.assertEqual(self.fake.paths("/register"), [])

    def test_keyring_failure_never_writes_api_key_to_state_file(self):
        comm = self._comm()
        with patch.object(
            keyring, "set_password",
            side_effect=keyring.errors.KeyringError(
                "Can't store password on keychain: (-61, 'Unknown Error')"
            ),
        ):
            comm.install_credentials("node_test01", "secret-api-key")

        state = json.loads(pathlib.Path("data", "cloud_state.json").read_text())
        self.assertEqual(state["node_id"], "node_test01")
        self.assertNotIn("api_key", state)
        # Encrypted-file fallback must keep identity across restart (issue #58).
        self.assertTrue(pathlib.Path("data", "credentials.enc").exists())
        self.assertEqual(comm.status.get("credential_store_backend"), "encrypted_file")
        self.assertTrue(comm.status.get("credential_store_ok"))
        self.assertIsNotNone(comm.status.get("credential_store_error"))
        self.assertIn("-61", str(comm.status.get("credential_store_error")))

        with patch.object(
            keyring, "get_password", return_value=None,
        ), patch.object(
            keyring, "set_password",
            side_effect=keyring.errors.KeyringError(
                "Can't store password on keychain: (-61, 'Unknown Error')"
            ),
        ):
            comm2 = self._comm()
        self.assertEqual(comm2.credentials()[0], "node_test01")
        self.assertEqual(comm2.credentials()[1], "secret-api-key")

    def test_registration_failure_backs_off_instead_of_hammering(self):
        self.fake.mode = "reject"
        comm = self._comm()
        self.assertFalse(comm._ensure_registered())
        n_after_first = len(self.fake.paths("/register"))
        # Immediate retries are suppressed by the backoff window.
        for _ in range(10):
            self.assertFalse(comm._ensure_registered())
        self.assertEqual(len(self.fake.paths("/register")), n_after_first)
        self.assertGreater(comm._register_next_attempt, 0)
        # Structured evidence of the failure exists.
        self.assertGreaterEqual(telemetry.counters().get("registration_failed", 0), 1)

    def test_rejected_credentials_clear_and_reregister(self):
        """A 401 'invalid node credentials' with no recovery_token available
        (e.g. a node registered before recovery_token existed) means the
        stored key is dead, not transiently unavailable, and there's no way
        to heal it in place — the node must drop it and register fresh
        rather than heartbeat against it forever (see _handle_unauthorized).
        See test_rejected_credentials_recover_via_recovery_token_without_losing_identity
        for the (now more common) case where recovery works instead."""
        keystore: dict = {}
        with patch.object(keyring, "set_password",
                          side_effect=lambda svc, acct, pw: keystore.__setitem__((svc, acct), pw)), \
             patch.object(keyring, "get_password",
                          side_effect=lambda svc, acct: keystore.get((svc, acct))), \
             patch.object(keyring, "delete_password",
                          side_effect=lambda svc, acct: keystore.pop((svc, acct), None)):
            comm = self._comm()
            self.assertTrue(comm._ensure_registered())
            self.assertTrue(keystore)
            # Simulate a pre-recovery-token node: no recovery_token on file,
            # so rekey isn't an option and the fallback path is what's tested.
            comm._recovery_token = ""
            keystore.pop(("the-telescope-node", "cloud-recovery-token"), None)

            self.fake.mode = "unauthorized"
            with self.assertRaises(RuntimeError):
                comm._post("/api/v1/nodes/heartbeat", {"conditions": {}, "state": {}})

            self.assertEqual(comm._node_id, "")
            self.assertEqual(comm._api_key, "")
            self.assertFalse(comm.status["registered"])
            self.assertEqual(keystore, {})
            state = json.loads(pathlib.Path("data", "cloud_state.json").read_text())
            self.assertEqual(state["node_id"], "")
            self.assertGreaterEqual(telemetry.counters().get("credentials_rejected", 0), 1)

            # Recovery: the next heartbeat cycle registers fresh instead of
            # retrying the dead key forever.
            self.fake.mode = "ok"
            self.fake.clear()
            # Wipe starts the same register backoff as a failed /register
            # (60s). The next heartbeat cycle is when we actually re-register;
            # skip the window the way a later cycle would.
            comm._register_next_attempt = 0.0
            self.assertTrue(comm._ensure_registered())
            self.assertNotEqual(comm._node_id, "")
            self.assertEqual(len(self.fake.paths("/register")), 1)

    def test_rejected_credentials_recover_via_recovery_token_without_losing_identity(self):
        """When a recovery_token is on hand, a rejected api_key must be healed
        in place -- same node_id, no re-registration, no lost history. This
        is the whole point of recovery_token: unlike the bare clear-and-
        reregister fallback above, the node never becomes a new, unlinked
        node that a human has to go re-attach in the app."""
        keystore: dict = {}
        with patch.object(keyring, "set_password",
                          side_effect=lambda svc, acct, pw: keystore.__setitem__((svc, acct), pw)), \
             patch.object(keyring, "get_password",
                          side_effect=lambda svc, acct: keystore.get((svc, acct))), \
             patch.object(keyring, "delete_password",
                          side_effect=lambda svc, acct: keystore.pop((svc, acct), None)):
            comm = self._comm()
            self.assertTrue(comm._ensure_registered())
            original_node_id = comm._node_id
            self.assertEqual(comm._recovery_token, "recovery_test01")

            # The api_key is now dead (e.g. a DB reset invalidated it), but the
            # recovery_token is a separate secret and still good. The failing
            # call itself still raises (same as the no-recovery-token case) --
            # what matters is that credentials are healed in place for the
            # *next* attempt, which the heartbeat loop makes ~1 cycle later.
            self.fake.mode = "unauthorized"
            self.fake.clear()
            with self.assertRaises(RuntimeError):
                comm._post("/api/v1/nodes/heartbeat", {"conditions": {}, "state": {}})

            self.assertEqual(comm._node_id, original_node_id)
            self.assertEqual(comm._api_key, "key_test01_rekeyed")
            self.assertTrue(comm.status["registered"])
            self.assertEqual(self.fake.paths("/register"), [])
            self.assertEqual(len(self.fake.paths("/rekey")), 1)
            self.assertGreaterEqual(telemetry.counters().get("credentials_rekeyed", 0), 1)

            # The rotated recovery_token survived too, in the keyring.
            self.assertEqual(
                keystore.get(("the-telescope-node", "cloud-recovery-token")),
                "recovery_test01_rotated")

            # And the very next heartbeat goes through cleanly on the healed
            # api_key -- no re-registration, no re-linking, nothing for a
            # human to notice.
            self.fake.mode = "ok"
            self.fake.clear()
            ok = comm._post("/api/v1/nodes/heartbeat", {"conditions": {}, "state": {}})
            self.assertEqual(ok, {"ok": True})
            self.assertEqual(self.fake.paths("/register"), [])

    def test_registration_outage_recovers_after_backoff(self):
        self.fake.mode = "http500"
        comm = self._comm()
        self.assertFalse(comm._ensure_registered())
        # Simulate the backoff window elapsing, then the cloud coming back.
        comm._register_next_attempt = 0.0
        self.fake.mode = "ok"
        self.assertTrue(comm._ensure_registered())

    # ── Upload queue ───────────────────────────────────────────────────────────

    def test_upload_failure_queues_to_disk_and_flushes_on_recovery(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        self.fake.mode = "http500"
        delivered = comm.submit_measurement(
            {"target_name": "T Tau", "magnitude": 10.2, "bjd": 2460000.5})
        self.assertFalse(delivered)
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(len(queue), 1)

        self.fake.mode = "ok"
        self.fake.clear()
        comm._flush_queue()
        self.assertEqual(len(self.fake.paths("/measurements")), 1)
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(queue, [])

    def test_unregistered_node_queues_measurements(self):
        self.fake.mode = "down"
        comm = self._comm()
        comm._register_next_attempt = float("inf")  # stay unregistered
        self.assertFalse(comm.submit_measurement({"target_name": "X", "bjd": 1.0}))
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(len(queue), 1)

    def test_corrupt_queue_file_does_not_crash(self):
        comm = self._comm()
        os.makedirs("data", exist_ok=True)
        with open(os.path.join("data", "cloud_upload_queue.json"), "w") as fh:
            fh.write('[{"measurement": truncated-by-cras')
        self.assertEqual(comm._load_queue(), [])
        # Enqueueing on top of the corrupt file recovers it.
        comm._enqueue({"measurement": {"target_name": "Y"}})
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(len(queue), 1)

    def test_queue_overflow_drops_oldest_and_leaves_evidence(self):
        comm = self._comm()
        from src import cloud_communicator as cc
        for i in range(cc._QUEUE_MAX + 5):
            comm._enqueue({"measurement": {"i": i}})
        queue = comm._load_queue()
        self.assertEqual(len(queue), cc._QUEUE_MAX)
        self.assertEqual(queue[0]["measurement"]["i"], 5)  # oldest dropped
        self.assertGreaterEqual(
            telemetry.counters().get("upload_queue_overflow", 0), 1)

    def test_flush_is_time_boxed_per_cycle(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        for i in range(60):
            comm._enqueue({"measurement": {"i": i}})
        self.fake.clear()
        comm._flush_queue(max_items=25)
        self.assertEqual(len(self.fake.paths("/measurements")), 25)
        self.assertEqual(len(comm._load_queue()), 35)  # rest kept for next cycle

    def test_flush_stops_at_first_failure_and_keeps_order(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        for i in range(5):
            comm._enqueue({"measurement": {"i": i}})
        self.fake.mode = "http500"
        comm._flush_queue()
        remaining = comm._load_queue()
        self.assertEqual([p["measurement"]["i"] for p in remaining], [0, 1, 2, 3, 4])

    def test_queue_save_is_atomic(self):
        comm = self._comm()
        comm._enqueue({"measurement": {"i": 1}})
        # The temp file must not linger, and the target must be valid JSON.
        self.assertFalse(os.path.exists(os.path.join("data", "cloud_upload_queue.json.tmp")))
        json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())

    # ── Incident forwarding ────────────────────────────────────────────────────

    def test_submit_incident_posts_structured_event(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        self.fake.clear()
        comm.submit_incident({"event": "slew_failed", "severity": "error",
                              "target": "T CrB", "detail": {"timeout_s": 180}})
        self.assertEqual(len(self.fake.paths("/incidents")), 1)
        req = [r for r in self.fake.requests if r["path"].endswith("/incidents")][0]
        self.assertEqual(req["body"]["incident_type"], "slew_failed")
        self.assertEqual(req["body"]["severity"], "error")

    def test_submit_incident_skipped_when_unregistered(self):
        self.fake.mode = "down"
        comm = self._comm()
        comm._register_next_attempt = float("inf")
        self.fake.clear()
        comm.submit_incident({"event": "x", "severity": "error"})  # no raise
        self.assertEqual(self.fake.paths("/incidents"), [])

    # ── Immediate heartbeat on device-state change ─────────────────────────────

    def test_request_heartbeat_wakes_loop_before_interval_elapses(self):
        """A device reconnect must not sit behind a stale cached cloud status
        for up to a full heartbeat interval — request_heartbeat() should cut
        the wait short."""
        import threading
        import time

        comm = self._comm()
        woke_at = {}

        def _wait():
            start = time.monotonic()
            comm._wait_or_kick(30)
            woke_at["elapsed"] = time.monotonic() - start

        t = threading.Thread(target=_wait)
        t.start()
        time.sleep(0.2)  # let the wait actually start blocking
        comm.request_heartbeat()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "wait_or_kick did not wake on request_heartbeat")
        self.assertLess(woke_at["elapsed"], 5.0)
        self.assertFalse(comm._kick.is_set(), "kick flag should be cleared after waking")


if __name__ == "__main__":
    unittest.main()


class CredentialStoreFallbackTest(TempCwdTestCase):
    """Issue #58: headless Mac keychain -61 must not orphan node identity."""

    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        # Reset module latches between tests.
        from src import credential_store as cs
        cs._backend = "keyring"
        cs._last_error = None

    def tearDown(self):
        telemetry.reset_for_tests()
        super().tearDown()

    def test_minus_61_falls_back_to_encrypted_file(self):
        from src import credential_store as cs
        with patch.object(
            keyring, "set_password",
            side_effect=keyring.errors.KeyringError(
                "Can't store password on keychain: (-61, 'Unknown Error')"
            ),
        ):
            backend = cs.set_password("cloud-api-key", "sekret")
        self.assertEqual(backend, "encrypted_file")
        with patch.object(keyring, "get_password", return_value=None):
            self.assertEqual(cs.get_password("cloud-api-key"), "sekret")
        snap = cs.status_snapshot()
        self.assertEqual(snap["credential_store_backend"], "encrypted_file")
        self.assertTrue(snap["credential_store_ok"])
        self.assertIn("-61", snap["credential_store_error"] or "")

    def test_is_keychain_denied_detects_minus_61(self):
        from src.credential_store import is_keychain_denied
        self.assertTrue(is_keychain_denied(
            keyring.errors.KeyringError("(-61, 'Unknown Error')")))
        self.assertTrue(is_keychain_denied(
            keyring.errors.KeyringError("errSecInvalidOwnerEdit")))
        self.assertFalse(is_keychain_denied(
            keyring.errors.KeyringError("temporary glitch")))
