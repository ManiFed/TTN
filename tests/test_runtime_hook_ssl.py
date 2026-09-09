#!/usr/bin/env python3
"""Frozen NodeAgent runtime-hook SSL cert wiring (issue #80)."""

from __future__ import annotations

import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RuntimeHookSslCertTest(unittest.TestCase):
    def _run_hook_with_meipass(self, meipass: Path, run_name: str) -> None:
        had_meipass = hasattr(sys, "_MEIPASS")
        old_meipass = getattr(sys, "_MEIPASS", None)
        try:
            sys._MEIPASS = str(meipass)
            runpy.run_path(str(ROOT / "build" / "runtime_hook.py"), run_name=run_name)
        finally:
            if had_meipass:
                sys._MEIPASS = old_meipass
            elif hasattr(sys, "_MEIPASS"):
                delattr(sys, "_MEIPASS")

    def test_sets_ssl_env_from_bundled_certifi(self):
        """Issue #80: frozen macOS needs SSL_CERT_FILE → bundled cacert.pem."""
        hook = ROOT / "build" / "runtime_hook.py"
        self.assertTrue(hook.is_file())

        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp)
            cert_dir = meipass / "certifi"
            cert_dir.mkdir()
            cacert = cert_dir / "cacert.pem"
            cacert.write_text("dummy-ca\n", encoding="utf-8")

            saved = {
                "SSL_CERT_FILE": os.environ.pop("SSL_CERT_FILE", None),
                "REQUESTS_CA_BUNDLE": os.environ.pop("REQUESTS_CA_BUNDLE", None),
                "PATH": os.environ.get("PATH"),
            }
            try:
                self._run_hook_with_meipass(meipass, "__runtime_hook_test__")
                self.assertEqual(os.environ.get("SSL_CERT_FILE"), str(cacert))
                self.assertEqual(os.environ.get("REQUESTS_CA_BUNDLE"), str(cacert))
                self.assertTrue(os.environ["PATH"].startswith(str(meipass)))
            finally:
                for key, val in saved.items():
                    if val is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = val

    def test_setdefault_preserves_operator_override(self):
        """Codex P2: restore SSL_* and PATH after the hook mutates them."""
        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp)
            cert_dir = meipass / "certifi"
            cert_dir.mkdir()
            (cert_dir / "cacert.pem").write_text("dummy\n", encoding="utf-8")

            saved = {
                "SSL_CERT_FILE": os.environ.get("SSL_CERT_FILE"),
                "REQUESTS_CA_BUNDLE": os.environ.get("REQUESTS_CA_BUNDLE"),
                "PATH": os.environ.get("PATH"),
            }
            os.environ["SSL_CERT_FILE"] = "/custom/cacert.pem"
            os.environ["REQUESTS_CA_BUNDLE"] = "/custom/cacert.pem"
            try:
                self._run_hook_with_meipass(meipass, "__runtime_hook_test2__")
                self.assertEqual(os.environ["SSL_CERT_FILE"], "/custom/cacert.pem")
                self.assertEqual(os.environ["REQUESTS_CA_BUNDLE"], "/custom/cacert.pem")
            finally:
                for key, val in saved.items():
                    if val is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = val


if __name__ == "__main__":
    unittest.main()
