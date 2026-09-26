"""cloud.help_chat._sanitize_patch: allowlist, not a denylist.

The system prompt (_PROJECT_CONTEXT) documents a specific list of "safe
config.yaml keys" the model may patch. The actual guardrail used to be a
denylist on secret-sounding key name fragments (password/api_key/secret/
token/auth/credential) with no check against that documented list at all --
so any other key, including things like cloud.url, passed straight through.
src/config_patch.py then deep-merges whatever survives here into the node's
real config.yaml with zero further validation.
"""

import unittest

from cloud import help_chat


class SanitizePatchTest(unittest.TestCase):
    def test_documented_safe_keys_pass_through(self):
        patch = {"cloud": {"auto_run_plans": True, "plan_poll_interval": 60},
                 "safety": {"park_at_dawn": True}}
        clean = help_chat._sanitize_patch(patch)
        self.assertEqual(clean, patch)

    def test_undocumented_non_secret_key_is_dropped(self):
        """The actual finding: cloud.url doesn't contain any blocked-name
        fragment, so the old denylist let it straight through. Repointing a
        node's cloud.url would redirect its authenticated traffic --
        api_key included -- to wherever the patch says."""
        patch = {"cloud": {"url": "http://attacker.example/"}}
        self.assertEqual(help_chat._sanitize_patch(patch), {})

    def test_secret_named_key_is_still_dropped(self):
        patch = {"cloud": {"api_key": "abc123"}}
        self.assertEqual(help_chat._sanitize_patch(patch), {})

    def test_unrelated_top_level_section_is_dropped(self):
        for bad in ({"scheduler": {"max_targets_per_night": 999}},
                    {"observatory": {"latitude": 89.9}},
                    {"aavso": {"dry_run": True}}):
            self.assertEqual(help_chat._sanitize_patch(bad), {}, bad)

    def test_mixed_patch_keeps_only_the_allowed_leaf(self):
        patch = {"cloud": {"auto_run_plans": True, "url": "http://evil"}}
        self.assertEqual(help_chat._sanitize_patch(patch),
                         {"cloud": {"auto_run_plans": True}})

    def test_every_documented_safe_key_is_individually_accepted(self):
        """Guards against _ALLOWED_PATCH_KEYS drifting out of sync with the
        list the model is actually told about in _PROJECT_CONTEXT."""
        for dotted in help_chat._ALLOWED_PATCH_KEYS:
            parts = dotted.split(".")
            patch = value = {}
            for p in parts[:-1]:
                value[p] = {}
                value = value[p]
            value[parts[-1]] = True
            self.assertEqual(help_chat._sanitize_patch(patch), patch, dotted)


if __name__ == "__main__":
    unittest.main()
