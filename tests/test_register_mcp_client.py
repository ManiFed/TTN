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
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from telescope_mcp import register_client as reg

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


class InstallerEntryTest(_Tmp):
    """What the installers actually register has to be launchable.

    A packaged build registers the agent binary directly; a source checkout
    registers an interpreter, which needs the module before the flags or the
    command cannot start at all.
    """

    def test_a_packaged_build_registers_the_agent_directly(self):
        reg.register("/Applications/TelescopeNetNode.app/Contents/MacOS/TelescopeNetNode",
                     DATA_DIR, self.path)
        entry = self.read()["mcpServers"]["telescope-net"]
        self.assertTrue(entry["command"].endswith("TelescopeNetNode"))
        self.assertEqual(entry["args"][0], "--mcp")

    def test_a_source_checkout_registers_the_module_too(self):
        reg.register("/usr/bin/python3", DATA_DIR, self.path,
                     prefix_args=["-m", "src.main_service"])
        entry = self.read()["mcpServers"]["telescope-net"]
        self.assertEqual(entry["args"][:3], ["-m", "src.main_service", "--mcp"])

    def test_windows_config_path_is_under_roaming_appdata(self):
        from unittest.mock import patch
        with patch("platform.system", return_value="Windows"), \
             patch.dict("os.environ", {"APPDATA": r"C:\Users\Someone\AppData\Roaming"}):
            path = str(reg.config_path()).replace("\\", "/")
        self.assertIn("AppData/Roaming/Claude/claude_desktop_config.json", path)

    def test_an_explicit_path_overrides_the_platform_default(self):
        """The Windows installer runs elevated, so an inherited %APPDATA% can
        belong to the elevating admin rather than the member. It therefore
        passes the path rather than letting it be inferred."""
        ok, msg = reg.register("agent.exe", DATA_DIR, self.path)
        self.assertTrue(ok)
        self.assertIn(str(self.path), msg)


class DiscoverabilityTest(_Tmp):
    """A member has to be told this exists.

    Nothing else in the product mentions the assistant, so if registration is
    silent the whole feature is invisible: it works perfectly and nobody ever
    uses it.
    """

    def test_the_message_says_where_it_registered(self):
        ok, msg = reg.register("agent", DATA_DIR, self.path)
        self.assertTrue(ok)
        self.assertIn("telescope-net", msg)

    def test_it_registers_even_when_the_assistant_is_absent(self):
        """Someone who installs Claude later should find it already there."""
        from unittest.mock import patch
        with patch.object(reg, "client_installed", return_value=False):
            ok, msg = reg.register("agent", DATA_DIR, self.path)
        self.assertTrue(ok)
        self.assertIn("telescope-net", self.read()["mcpServers"])
        self.assertIn("not installed yet", msg)

    def test_it_does_not_claim_success_at_someone_who_has_no_assistant(self):
        from unittest.mock import patch
        with patch.object(reg, "client_installed", return_value=True):
            _, msg = reg.register("agent", DATA_DIR, self.path)
        self.assertNotIn("not installed yet", msg)


class MultiClientTest(_Tmp):
    """The telescope should be there whatever assistant someone uses.

    Registering only Claude quietly made this a Claude feature. Every client
    here reads the same {"mcpServers": {...}} shape, so one entry serves all of
    them -- the only real difference is where the file lives, and that editors
    keep a great deal of unrelated settings in it, which is why register()
    merges rather than writes.
    """

    def test_claude_desktop_is_the_only_registered_client(self):
        """One supported path. Cursor, Windsurf and Claude Code all work if
        configured by hand, but every extra one is another setup story to
        describe, test and support -- and another sentence a member has to
        decide whether applies to them."""
        self.assertEqual(list(reg.CLIENTS), ["Claude Desktop"])

    def test_chatgpt_is_not_registered_locally(self):
        """ChatGPT reaches MCP servers only as remote connectors over HTTPS, so
        it never launches anything locally and cannot see a telescope on a home
        network. Writing it a config would create a file nothing reads -- and
        would let the installer claim it was set up when it was not."""
        self.assertNotIn("ChatGPT Desktop", reg.CLIENTS)
        self.assertNotIn("ChatGPT", reg.CLIENTS)

    def test_there_is_something_to_tell_a_chatgpt_user(self):
        self.assertIn("remote", reg.REMOTE_ONLY_NOTE.lower())
        self.assertIn("Claude Desktop", reg.REMOTE_ONLY_NOTE)

    def test_every_client_resolves_a_path_on_every_platform(self):
        from unittest.mock import patch
        for system in ("Darwin", "Windows", "Linux"):
            with patch("platform.system", return_value=system):
                for name in reg.CLIENTS:
                    self.assertTrue(str(reg.config_path(name)),
                                    f"{name} has no path on {system}")

    def test_an_unknown_client_falls_back_rather_than_raising(self):
        self.assertTrue(str(reg.config_path("Some Future Assistant")))

    def test_register_all_writes_every_config(self):
        from unittest.mock import patch
        written = {}

        def fake_register(command, data_dir, path, prefix_args=None):
            written[str(path)] = True
            return True, f"Registered in {path}."

        with patch.object(reg, "register", side_effect=fake_register):
            ok, msg = reg.register_all("agent", DATA_DIR)
        self.assertTrue(ok)
        self.assertEqual(len(written), len(reg.CLIENTS))

    def test_an_unwritable_config_is_reported_rather_than_hidden(self):
        """With one supported client its failure is the whole failure, and the
        installer has to be able to say so."""
        from unittest.mock import patch
        with patch.object(reg, "register",
                          return_value=(False, "is not valid JSON. Refusing "
                                               "to touch it.")):
            ok, msg = reg.register_all("agent", DATA_DIR)
        self.assertFalse(ok)
        self.assertIn("Could not write", msg)

    def test_it_registers_clients_that_are_not_installed_yet(self):
        """An entry costs nothing, and someone who installs an assistant next
        week should not have to re-run an installer they have thrown away."""
        from unittest.mock import patch
        with patch.object(reg, "installed_clients", return_value=[]), \
             patch.object(reg, "register", return_value=(True, "Registered.")):
            ok, msg = reg.register_all("agent", DATA_DIR)
        self.assertTrue(ok)
        self.assertIn("No assistant found yet", msg)

    def test_it_names_what_is_actually_installed(self):
        from unittest.mock import patch
        with patch.object(reg, "installed_clients", return_value=["Cursor"]), \
             patch.object(reg, "register", return_value=(True, "Registered.")):
            _, msg = reg.register_all("agent", DATA_DIR)
        self.assertIn("Cursor", msg)

    def test_removal_covers_every_client(self):
        from unittest.mock import patch
        removed = []
        with patch.object(reg, "remove",
                          side_effect=lambda p: (removed.append(str(p)), (True, "gone"))[1]):
            ok, msg = reg.remove_all()
        self.assertTrue(ok)
        self.assertEqual(len(removed), len(reg.CLIENTS))


class SelfHealTest(_Tmp):
    """Registering once is not enough.

    Claude Desktop keeps its own settings in the same file and rewrites it from
    memory, so an entry written while it is running is dropped again the next
    time it saves -- silently. The first real install hit exactly that: the
    telescope was registered, Claude overwrote the file, and the member asked
    to connect their telescope and got generic advice from an assistant that
    had no tools at all.
    """

    def _ensure(self):
        from unittest.mock import patch
        with patch.object(reg, "config_path", return_value=self.path):
            return reg.ensure_registered("agent", DATA_DIR)

    def test_it_puts_the_entry_back_after_claude_drops_it(self):
        self.write({"mcpServers": {"telescope-net": {"command": "agent"}}})
        # What Claude Desktop leaves behind: its own keys, ours gone.
        self.write({"coworkUserFilesPath": "/x", "preferences": {}})
        self.assertTrue(self._ensure())
        self.assertIn("telescope-net", self.read()["mcpServers"])

    def test_it_preserves_whatever_claude_wrote(self):
        self.write({"coworkUserFilesPath": "/x", "preferences": {"theme": "dark"}})
        self._ensure()
        data = self.read()
        self.assertEqual(data["coworkUserFilesPath"], "/x")
        self.assertEqual(data["preferences"], {"theme": "dark"})

    def test_it_does_nothing_when_the_entry_is_already_right(self):
        self._ensure()
        before = self.path.read_text()
        self.assertFalse(self._ensure(), "rewrote an already-correct config")
        self.assertEqual(self.path.read_text(), before)

    def test_it_updates_a_stale_entry(self):
        """An upgrade that moves the binary must not leave a dead command."""
        reg.register("/old/agent", DATA_DIR, self.path)
        self.assertTrue(self._ensure())
        self.assertEqual(self.read()["mcpServers"]["telescope-net"]["command"],
                         "agent")

    def test_it_leaves_other_servers_alone(self):
        self.write({"mcpServers": {"filesystem": {"command": "npx"}}})
        self._ensure()
        self.assertIn("filesystem", self.read()["mcpServers"])

    def test_it_refuses_a_config_it_cannot_parse(self):
        """Never rewrite a file we cannot read -- it may list servers we
        cannot see."""
        self.write('{"mcpServers": {"filesystem"')
        self.assertFalse(self._ensure())
        self.assertIn('{"mcpServers": {"filesystem"', self.path.read_text())

    def test_a_member_who_opted_out_stays_out(self):
        """Removing the telescope from Claude is a decision, not a fault to
        repair every five minutes."""
        (self.path.parent / reg.OPT_OUT_MARKER).write_text("")
        self.assertFalse(self._ensure())
        self.assertFalse(self.path.exists())
