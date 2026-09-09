#!/usr/bin/env python3
"""Frozen NodeAgent PyInstaller hiddenimport regressions."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PhotutilsHiddenImportsTest(unittest.TestCase):
    def test_spec_lists_geometry_core(self):
        """Issue #78: CircularAperture needs photutils.geometry.core in the bundle."""
        spec = (ROOT / "build" / "node_agent.spec").read_text(encoding="utf-8")
        self.assertIn('"photutils.geometry"', spec)
        self.assertIn('"photutils.geometry.core"', spec)
        # Codex P2: require the actual photutils collection call, not just the
        # import name / unrelated substring.
        self.assertRegex(
            spec,
            r"""collect_submodules\(\s*["']photutils["']\s*\)""",
            msg="build/node_agent.spec must call collect_submodules('photutils')",
        )
        self.assertIn('"photutils.psf"', spec)


if __name__ == "__main__":
    unittest.main()
