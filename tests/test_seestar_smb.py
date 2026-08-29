#!/usr/bin/env python3
"""Seestar SMB ingest: share name, URL encoding, reuse of Finder mounts.

The overnight report on node_6313f027 (issue #35) failed guest-mounting
//guest:@host/seestar — that share does not exist. ZWO documents
"EMMC Images" with guest access and FITS under MyWorks/.

Run with:  python3 -m pytest tests/test_seestar_smb.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from src import seestar_smb


class ShareNameTest(unittest.TestCase):

    def test_share_is_emmc_images_not_seestar(self):
        self.assertEqual(seestar_smb.SHARE_NAME, "EMMC Images")
        self.assertNotEqual(seestar_smb.SHARE_NAME.lower(), "seestar")

    def test_macos_url_encodes_the_space_and_uses_guest(self):
        url = seestar_smb.smb_share_url("172.22.6.32")
        self.assertEqual(url, "//guest:@172.22.6.32/EMMC%20Images")
        self.assertNotIn("/seestar", url)

    def test_linux_unc_keeps_the_space_as_one_argument(self):
        unc = seestar_smb.cifs_unc("172.22.6.32")
        self.assertEqual(unc, "//172.22.6.32/EMMC Images")
        cmd = seestar_smb.mount_cmd("172.22.6.32", "/data/mounts/seestar", "Linux")
        self.assertIsNotNone(cmd)
        self.assertIn(unc, cmd)
        self.assertTrue(any("guest" in a for a in cmd))

    def test_macos_mount_cmd_uses_mount_smbfs_guest(self):
        cmd = seestar_smb.mount_cmd("10.0.0.1", "/tmp/mnt", "Darwin")
        self.assertEqual(cmd[0], "mount_smbfs")
        self.assertIn("-N", cmd)
        self.assertIn("//guest:@10.0.0.1/EMMC%20Images", cmd)

    def test_unsupported_platform_has_no_command(self):
        self.assertIsNone(seestar_smb.mount_cmd("10.0.0.1", "/tmp/mnt", "Windows"))


class ExistingVolumeTest(unittest.TestCase):

    def test_reuses_finder_emmc_images_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            vol = Path(tmp) / "EMMC Images"
            (vol / "MyWorks").mkdir(parents=True)
            found = seestar_smb.find_existing_share(roots=(tmp,))
            self.assertEqual(found, str(vol))

    def test_ignores_empty_named_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Seestar").mkdir()
            self.assertIsNone(seestar_smb.find_existing_share(roots=(tmp,)))

    def test_watch_dir_prefers_myworks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MyWorks").mkdir()
            self.assertEqual(seestar_smb.watch_dir(str(root)), str(root / "MyWorks"))

    def test_watch_dir_falls_back_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(seestar_smb.watch_dir(tmp), tmp)

    def test_unusable_missing_path(self):
        self.assertFalse(seestar_smb._usable(Path("/definitely/not/a/seestar")))
