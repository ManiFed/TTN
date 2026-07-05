"""
Apply cloud-queued config.yaml patches safely on the node agent.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path

import yaml

logger = logging.getLogger("config_patch")

CONFIG_PATH = Path("config.yaml")


def _deep_merge(base: dict, patch: dict) -> dict:
    out = deepcopy(base)
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def apply_config_patch(patch: dict, path: Path = CONFIG_PATH) -> None:
    """Merge *patch* into config.yaml and write atomically."""
    if not patch:
        return
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")

    with open(path) as fh:
        current = yaml.safe_load(fh) or {}

    if not isinstance(current, dict):
        raise ValueError("config.yaml root must be a mapping")

    merged = _deep_merge(current, patch)
    raw = yaml.dump(
        merged,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    # Validate round-trip
    yaml.safe_load(raw)

    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(path)
    logger.info("config.yaml updated from cloud patch: %s", list(patch.keys()))