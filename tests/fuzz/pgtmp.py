"""Ephemeral PostgreSQL cluster for fuzzing.

Spins up a throwaway postgres in a temp dir on a unix socket (no TCP), with
durability off for speed. The cloud code talks to the exact same engine it
uses in production instead of a mocked db layer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_CANDIDATE_BIN_DIRS = [
    "/opt/homebrew/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/local/opt/postgresql@17/bin",
    "/usr/lib/postgresql/17/bin",
]


def available() -> bool:
    """Whether a local PostgreSQL exists to spin an ephemeral cluster from.

    Tests that need a real database should skip on this rather than error.
    CI runners without PostgreSQL installed would otherwise turn every such
    test red, which buries the failures that actually mean something.
    """
    try:
        _pg_bin()
        return True
    except RuntimeError:
        return False


def _pg_bin() -> str:
    for d in _CANDIDATE_BIN_DIRS:
        if os.path.isfile(os.path.join(d, "initdb")):
            return d
    if shutil.which("initdb"):
        return os.path.dirname(shutil.which("initdb"))
    raise RuntimeError("PostgreSQL binaries not found (initdb)")


class TempPostgres:
    """Context manager: ephemeral postgres cluster; .dsn is a psycopg2 URL."""

    def __init__(self, base_dir: str | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="fuzzpg_", dir=base_dir))
        self.data = self.dir / "data"
        self.sock = self.dir / "sock"
        self.log = self.dir / "postgres.log"
        self.dsn = ""
        self._bin = _pg_bin()

    def _run(self, *argv: str) -> None:
        # LC_ALL must be a valid locale or the macOS postmaster aborts with
        # "postmaster became multithreaded during startup".
        env = dict(os.environ, LC_ALL="C", LANG="C")
        subprocess.run(argv, check=True, capture_output=True, timeout=60, env=env)

    def __enter__(self) -> "TempPostgres":
        # The postmaster is a separate process: if this process dies without
        # __exit__, it would outlive us and leak shared memory until the
        # machine runs out (initdb then fails fleet-wide). atexit covers
        # normal interpreter shutdown; harness teardown covers the rest.
        import atexit
        atexit.register(self.__exit__, None, None, None)
        self.sock.mkdir()
        self._run(os.path.join(self._bin, "initdb"), "-D", str(self.data),
                  "-U", "fuzz", "--auth=trust", "-E", "UTF8", "--no-sync")
        opts = (f"-c listen_addresses='' "
                f"-c unix_socket_directories='{self.sock}' "
                f"-c fsync=off -c synchronous_commit=off -c full_page_writes=off")
        self._run(os.path.join(self._bin, "pg_ctl"), "-D", str(self.data),
                  "-l", str(self.log), "-o", opts, "-w", "start")
        self._run(os.path.join(self._bin, "createdb"),
                  "-h", str(self.sock), "-U", "fuzz", "fuzz")
        self.dsn = f"postgresql://fuzz@/fuzz?host={self.sock}"
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._run(os.path.join(self._bin, "pg_ctl"), "-D", str(self.data),
                      "-m", "immediate", "stop")
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)
