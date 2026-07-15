"""Dedicated process entry point for NASA GCN Kafka consumption."""

import logging
import os
import time

from cloud import db
from cloud.gcn_consumer import GCNConsumerService
from cloud.main import load_config


def main() -> None:
    config = load_config(os.environ.get("CLOUD_CONFIG", "cloud/config.production.yaml"))
    logging.basicConfig(
        level=(config.get("logging") or {}).get("level", "INFO"),
        format=(config.get("logging") or {}).get(
            "format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    db.init((config.get("database") or {}).get("url", ""))
    service = GCNConsumerService(config)
    service.start()
    while True:
        time.sleep(30)


if __name__ == "__main__":
    main()
