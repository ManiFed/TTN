"""Shared helpers for the reliability gauntlet."""

import os
import shutil
import tempfile
import unittest


class TempCwdTestCase(unittest.TestCase):
    """Runs each test in a fresh temporary working directory.

    The node agent addresses config.yaml, data/ and logs/ relative to its
    working directory, so pointing cwd at a scratch dir isolates every
    filesystem fault we inject.
    """

    def setUp(self):
        super().setUp()
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="gauntlet_")
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    def write(self, relpath: str, content: str) -> str:
        path = os.path.join(self._tmp, relpath)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path
