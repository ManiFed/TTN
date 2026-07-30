"""
Minimal YAML config loader for the realtime service.

Deliberately does not import cloud.main.load_config: that module imports
cloud.server at load time (the full Flask app, scoring, chorus, etc.), which
pulls in dependencies (e.g. defusedxml) that aren't in realtime/requirements.txt
by design -- this service only needs flask/gevent/psycopg2.
"""

import os
import re
from pathlib import Path

import yaml

_DEFAULT_CONFIG = Path(__file__).parent.parent / "cloud" / "config.yaml"


def _expand_env(obj):
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def load_config(path=None) -> dict:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    try:
        with open(cfg_path) as fh:
            raw = yaml.safe_load(fh) or {}
        return _expand_env(raw)
    except FileNotFoundError:
        return {}
