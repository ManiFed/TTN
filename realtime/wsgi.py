"""
Gunicorn entry point for the realtime SSE gateway.

    gunicorn -k gevent -w 1 --bind 0.0.0.0:$PORT realtime.wsgi:app

The gevent worker makes the many concurrent, mostly-idle SSE connections cheap
(one greenlet each) instead of pinning OS threads. psycogreen makes psycopg2's
LISTEN socket cooperative under gevent so the notify loop never blocks the hub.
"""

import os

# Monkey-patch before anything imports sockets/psycopg2.
try:
    from gevent import monkey
    monkey.patch_all()
    from psycogreen.gevent import patch_psycopg
    patch_psycopg()
except Exception:  # pragma: no cover - dev without gevent installed
    pass

import logging

from cloud.main import load_config
from realtime.app import create_app

_config = load_config(os.environ.get("CLOUD_CONFIG", "cloud/config.production.yaml"))

logging.basicConfig(
    level=_config.get("logging", {}).get("level", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = create_app(_config)
