"""
Deprecated: superseded by cloud/ingest_worker.py.

The contribution processor was generalized into a staged pipeline
(triage → solve → extract → ingest) that plate-solves frames arriving without a
WCS. This module re-exports the new entry points so any external caller or
pinned import keeps working.
"""

from cloud.ingest_worker import (  # noqa: F401
    process_pending,
    prune_contrib_files,
)
