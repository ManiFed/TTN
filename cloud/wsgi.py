"""
Gunicorn entry point for production.

    gunicorn -w 1 --threads 8 --timeout 60 --bind 0.0.0.0:$PORT cloud.wsgi:app

Single worker + threads: keeps background loops deduplicated while still
handling concurrent HTTP requests.
"""

import logging
import os

from cloud import db
from cloud.main import load_config, start_background_loops
from cloud.server import create_app

_config_path = os.environ.get("CLOUD_CONFIG", "cloud/config.production.yaml")
_config = load_config(_config_path)

logging.basicConfig(
    level=_config.get("logging", {}).get("level", "INFO"),
    format=_config.get("logging", {}).get(
        "format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ),
)

db.init(_config.get("database", {}).get("url", ""))

# Guard against a second service accidentally pointing at this entrypoint
# (e.g. a misconfigured Railway config-file path) and running the full
# scoring/planning/ingest pipeline a second time against the same database.
if os.environ.get("RUN_BACKGROUND_LOOPS", "1") == "1":
    start_background_loops(_config)
else:
    logging.getLogger("cloud.wsgi").warning(
        "RUN_BACKGROUND_LOOPS=0 — background loops disabled on this service")

app = create_app(_config)
