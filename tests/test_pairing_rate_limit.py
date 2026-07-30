"""Pairing-claim rate limiting must stop enumeration without stopping members.

The endpoint hands out live node credentials to whoever presents the right
pairing token, so it has to resist guessing. But the legitimate client is a
node agent that polls ONE token every 30 s for as long as it takes its owner
to get around to linking it — which looks like sustained failed traffic.

Counting total misses conflated the two and punished the honest case. These
tests pin both directions so a future tightening can't quietly lock members
out again.
"""

import unittest
from unittest.mock import patch

import cloud.server as server


class _Req:
    """Minimal stand-in for flask.request for the source-IP helper."""

    def __init__(self, forwarded="", remote="203.0.113.9"):
        self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        self.remote_addr = remote


class ClaimSourceTest(unittest.TestCase):
    def test_uses_proxy_appended_hop_not_client_supplied_one(self):
        """Only the rightmost XFF entry is ours; the rest is caller input."""
        with patch.object(server, "request",
                          _Req(forwarded="1.1.1.1, 2.2.2.2, 198.51.100.7")):
            self.assertEqual(server._claim_source_ip(), "198.51.100.7")

    def test_spoofed_header_cannot_change_the_source(self):
        spoofs = [f"10.0.0.{i}" for i in range(5)]
        seen = set()
        for spoof in spoofs:
            with patch.object(server, "request",
                              _Req(forwarded=f"{spoof}, 198.51.100.7")):
                seen.add(server._claim_source_ip())
        self.assertEqual(
            seen, {"198.51.100.7"},
            "varying X-Forwarded-For changed the rate-limit key — an "
            "enumerating client could reset its budget with a header")

    def test_falls_back_to_remote_addr(self):
        with patch.object(server, "request", _Req(remote="192.0.2.4")):
            self.assertEqual(server._claim_source_ip(), "192.0.2.4")


class ClaimBudgetTest(unittest.TestCase):
    def setUp(self):
        server._pair_claim_misses.clear()

    tearDown = setUp

    def _poll(self, source, token):
        if server._pair_claim_limited(source):
            return False
        server._pair_claim_record_miss(source, token)
        return True

    def test_one_node_polling_for_hours_is_never_limited(self):
        # 30 s cadence for 6 h = 720 polls of a single token.
        for _ in range(720):
            self.assertTrue(
                self._poll("198.51.100.7", "VEGA-4821"),
                "a node waiting to be linked got rate-limited while polling "
                "its own token — it would miss the credentials its owner "
                "pushed")

    def test_many_telescopes_behind_one_router_are_never_limited(self):
        """A household, school, or club shares one public address."""
        tokens = [f"NOVA-{1000 + i}" for i in range(12)]
        for _ in range(60):                      # 30 min of polling each
            for token in tokens:
                self.assertTrue(
                    self._poll("198.51.100.7", token),
                    f"{len(tokens)} telescopes on one connection tripped the "
                    "pairing limit")

    def test_enumerating_distinct_tokens_is_limited(self):
        allowed = 0
        for i in range(500):
            if not self._poll("198.51.100.7", f"MIRA-{1000 + i}"):
                break
            allowed += 1
        self.assertLessEqual(
            allowed, server._PAIR_CLAIM_MAX_TOKENS,
            "token enumeration was not throttled")
        self.assertFalse(server._pair_claim_limited("203.0.113.1") is True,
                         "one abuser must not rate-limit a different source")

    def test_per_source_memory_is_bounded(self):
        for i in range(server._PAIR_CLAIM_MAX_PER_SOURCE * 3):
            server._pair_claim_record_miss("198.51.100.7", f"T-{i}")
        self.assertLessEqual(
            len(server._pair_claim_misses["198.51.100.7"]),
            server._PAIR_CLAIM_MAX_PER_SOURCE,
            "an enumerating client could grow the tracking dict without bound")


if __name__ == "__main__":
    unittest.main()
