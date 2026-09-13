"""Identity verification for ALPACA discovery (issue #97).

A Starfront-night LAN answers the discovery broadcast with real hardware
mixed in among decoys: N.I.N.A.'s built-in Alpaca server (:32330) and the
ASCOM/Alpaca simulator (:32323). ``is_verified_seestar`` is the gate that
keeps a reconnect from ever treating one of those as the telescope.
"""

import unittest

from alpaca.discovery import is_verified_seestar


class IsVerifiedSeestarTest(unittest.TestCase):
    def test_accepts_seestar_s50(self):
        self.assertTrue(is_verified_seestar("Seestar S50 Telescope"))

    def test_accepts_seestar_s30prosf(self):
        self.assertTrue(is_verified_seestar("Seestar S30PROSF Telescope"))

    def test_accepts_zwo_branded_name(self):
        self.assertTrue(is_verified_seestar("ZWO Seestar Alpaca Driver"))

    def test_accepts_case_insensitively(self):
        self.assertTrue(is_verified_seestar("SEESTAR S50"))

    def test_rejects_nina(self):
        self.assertFalse(is_verified_seestar("N.I.N.A. Telescope Simulator"))
        self.assertFalse(is_verified_seestar("NINA Alpaca Device"))

    def test_rejects_ascom_simulator(self):
        self.assertFalse(is_verified_seestar("ASCOM Simulator Telescope"))

    def test_rejects_alpaca_simulator(self):
        self.assertFalse(is_verified_seestar("Alpaca Simulator"))

    def test_rejects_generic_simulator(self):
        self.assertFalse(is_verified_seestar("Telescope Simulator"))

    def test_rejects_blank_or_unknown_name(self):
        self.assertFalse(is_verified_seestar(""))
        self.assertFalse(is_verified_seestar(None))
        self.assertFalse(is_verified_seestar("Some Other Mount"))

    def test_nina_naming_seestar_is_still_rejected(self):
        # Belt and suspenders: a decoy that happens to mention "seestar" in
        # its string but is clearly NINA must still be rejected -- reject
        # patterns take priority over accept patterns.
        self.assertFalse(is_verified_seestar("N.I.N.A. (Seestar profile)"))


if __name__ == "__main__":
    unittest.main()
