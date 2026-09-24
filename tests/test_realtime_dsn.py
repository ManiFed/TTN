"""realtime/app.py must resolve the LISTEN thread's DSN the same way it
resolves the main DB pool's DSN.

create_app() used to init the main pool config-first (env fallback) but
start the LISTEN thread env-first (config fallback) -- the opposite order,
for what has to be the same database. In a dev setup where cloud/config.yaml
hardcodes a local DSN and DATABASE_URL also happens to be set in the shell,
the main pool and the LISTEN connection would silently point at two
different databases: node auth and health checks work fine, but live
dispatch events pushed via pg_notify() never reach the SSE fan-out.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from realtime import app as realtime_app


class DsnConsistencyTest(unittest.TestCase):
    def test_create_app_uses_the_same_dsn_for_db_and_listener(self):
        config = {"database": {"url": "postgresql://config-value/db"}}
        with patch.object(realtime_app.db, "init") as mock_init, \
             patch.object(realtime_app.threading, "Thread") as mock_thread, \
             patch.dict("os.environ", {"DATABASE_URL": "postgresql://env-value/db"}):
            realtime_app.create_app(config)

        db_dsn = mock_init.call_args[0][0]
        listener_dsn = mock_thread.call_args.kwargs["args"][0]
        self.assertEqual(db_dsn, listener_dsn,
                         "db.init() and the LISTEN thread must watch the same database")

    def test_attach_to_api_uses_the_same_precedence_as_create_app(self):
        config = {"database": {"url": "postgresql://config-value/db"}}
        api_app = MagicMock()
        with patch.object(realtime_app.threading, "Thread") as mock_thread, \
             patch.dict("os.environ", {"DATABASE_URL": "postgresql://env-value/db"}):
            realtime_app.attach_to_api(api_app, config)

        listener_dsn = mock_thread.call_args.kwargs["args"][0]
        self.assertEqual(listener_dsn, "postgresql://config-value/db",
                         "config value must win, matching db.init()'s own "
                         "config-first/env-fallback precedence")


if __name__ == "__main__":
    unittest.main()
