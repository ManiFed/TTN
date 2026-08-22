#!/usr/bin/env python3
"""Registering with Claude Desktop must never damage a config we don't own.

claude_desktop_config.json belongs to the member, not to us. It may list other
MCP servers they depend on and settings we know nothing about. The installer
runs this unattended, as root, on a machine we cannot see -- so the failure to
design against is not "registration didn't work", it is "registration worked
and silently deleted the member's other tools".

Hence: merge one key, keep a backup, write atomically, and refuse outright when
the existing file cannot be parsed.

Run with:  python3 -m pytest tests/test_register_mcp_client.py
"""

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "register_mcp_client", REPO / "scripts" / "register_mcp_client.py")
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)

COMMAND = "/Applications/TelescopeNetNode.app/Contents/MacOS/TelescopeNetNode"
DATA_DIR = "/Users/someone/Library/Application Support/TelescopeNet/NodeAgent"


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "claude_desktop_config.json"

    def write(self, obj_or_text):
        self.path.write_text(obj_or_text if isinstance(obj_or_text, str)
                             else json.dumps(obj_or_text))

    def read(self):
        return json.loads(self.path.read_text())

    def register(self):
        return reg.register(COMMAND, DATA_DIR, self.path)


class FreshInstallTest(_Tmp):

    def test_it_creates_the_config_when_there_is_none(self):
        ok, msg = self.register()
        self.assertTrue(ok, msg)
        self.assertEqual(list(self.read()["mcpServers"]), ["telescope-net"])

    def test_it_creates_missing_parent_directories(self):
        self.path = Path(self.dir.name) / "Claude" / "claude_desktop_config.json"
        ok, _ = self.register()
        self.assertTrue(ok)
        self.assertTrue(self.path.exists())

    def test_the_entry_launches_the_agent_in_mcp_mode(self):
        self.register()
        entry = self.read()["mcpServers"]["telescope-net"]
        self.assertEqual(entry["command"], COMMAND)
        self.assertIn("--mcp", entry["args"])
        self.assertIn(DATA_DIR, entry["args"])


class DoNoHarmTest(_Tmp):
    """The tests that matter: the member's own config survives intact."""

    def test_other_mcp_servers_are_preserved(self):
        self.write({"mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@mcp/filesystem"]},
            "github": {"command": "gh-mcp"},
        }})
        ok, msg = self.register()
        self.assertTrue(ok)
        servers = self.read()["mcpServers"]
        self.assertEqual(servers["filesystem"]["command"], "npx")
        self.assertEqual(servers["github"]["command"], "gh-mcp")
        self.assertIn("telescope-net", servers)
        self.assertIn("filesystem", msg, "the report should say what it kept")

    def test_unrelated_top_level_settings_are_preserved(self):
        self.write({"theme": "dark", "globalShortcut": "Cmd+Shift+Space"})
        self.register()
        data = self.read()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["globalShortcut"], "Cmd+Shift+Space")

    def test_a_malformed_config_is_refused_not_overwritten(self):
        """Rewriting it would delete MCP servers we cannot even read."""
        original = '{"mcpServers": {"filesystem": {"command": "npx"'   # truncated
        self.write(original)
        ok, msg = self.register()
        self.assertFalse(ok)
        self.assertEqual(self.path.read_text(), original, "the file was modified")
        self.assertIn("Refusing", msg)
        self.assertIn("delete them", msg)

    def test_a_non_object_config_is_refused(self):
        self.write("[1, 2, 3]")
        ok, msg = self.register()
        self.assertFalse(ok)
        self.assertIn("Refusing", msg)

    def test_a_non_object_mcpservers_key_is_refused(self):
        self.write({"mcpServers": "not an object"})
        ok, msg = self.register()
        self.assertFalse(ok)
        self.assertIn("Refusing", msg)

    def test_an_empty_file_is_treated_as_no_config(self):
        self.write("")
        ok, _ = self.register()
        self.assertTrue(ok)
        self.assertIn("telescope-net", self.read()["mcpServers"])

    def test_a_backup_is_kept_of_whatever_was_there(self):
        self.write({"mcpServers": {"filesystem": {"command": "npx"}}})
        self.register()
        backup = self.path.with_suffix(self.path.suffix + ".backup")
        self.assertTrue(backup.exists())
        self.assertIn("filesystem", json.loads(backup.read_text())["mcpServers"])

    def test_no_temporary_file_is_left_behind(self):
        self.register()
        self.assertFalse(self.path.with_suffix(self.path.suffix + ".tmp").exists())


class IdempotenceTest(_Tmp):

    def test_running_twice_changes_nothing_the_second_time(self):
        """The installer runs on every upgrade."""
        self.register()
        first = self.path.read_text()
        ok, msg = self.register()
        self.assertTrue(ok)
        self.assertIn("Already registered", msg)
        self.assertEqual(self.path.read_text(), first)

    def test_an_upgrade_that_moves_the_binary_updates_the_entry(self):
        reg.register("/old/path/agent", DATA_DIR, self.path)
        ok, _ = self.register()
        self.assertTrue(ok)
        self.assertEqual(self.read()["mcpServers"]["telescope-net"]["command"],
                         COMMAND)


class RemovalTest(_Tmp):

    def test_removal_takes_only_our_key(self):
        self.write({"mcpServers": {"filesystem": {"command": "npx"}},
                    "theme": "dark"})
        self.register()
        ok, msg = reg.remove(self.path)
        self.assertTrue(ok, msg)
        data = self.read()
        self.assertNotIn("telescope-net", data["mcpServers"])
        self.assertIn("filesystem", data["mcpServers"])
        self.assertEqual(data["theme"], "dark")

    def test_removing_when_absent_is_not_an_error(self):
        """Uninstall runs whether or not registration ever happened."""
        self.write({"mcpServers": {"filesystem": {"command": "npx"}}})
        ok, msg = reg.remove(self.path)
        self.assertTrue(ok)
        self.assertIn("nothing to remove", msg)

    def test_removing_with_no_config_at_all_is_not_an_error(self):
        ok, _ = reg.remove(self.path)
        self.assertTrue(ok)

    def test_removal_refuses_a_malformed_config(self):
        self.write('{"broken')
        ok, _ = reg.remove(self.path)
        self.assertFalse(ok)


class PlatformPathTest(unittest.TestCase):

    def test_each_platform_resolves_a_plausible_path(self):
        from unittest.mock import patch
        for system, fragment in (("Darwin", "Application Support/Claude"),
                                 ("Windows", "Claude"),
                                 ("Linux", ".config/Claude")):
            with patch("platform.system", return_value=system):
                self.assertIn(fragment.replace("/", "\\") if system == "Windows"
                              and "\\" in str(reg.config_path()) else fragment,
                              str(reg.config_path()).replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
