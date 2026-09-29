"""
etl_common.py

Shared constants and helpers for the DWH ETL pipeline:

    extractor.py -> delta_checker.py -> (staging dump) -> transformer.py

driven by main_orchestrator.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# File layout (relative to the working directory, like chatbot_metadata.db)
# --------------------------------------------------------------------------- #

STAGING_PATH = Path("staging_superset_data.json")  # raw dump (backup / staging layer)
READY_PATH = Path("ready_for_vector_db.json")      # chunked documents for embedding
STATE_PATH = Path("delta_state.json")              # last *committed* hash
REPORT_PATH = Path("extraction_report.json")       # per-run extraction summary

# Bump this whenever the transformer's output format changes. Without it, a
# run whose data is identical would be halted by the delta check and the new
# chunking would never be produced.
TRANSFORM_VERSION = 1


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, obj: Any, indent: Optional[int] = 2) -> None:
    """Write via temp file + rename, so a crash never leaves half-written JSON."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent, default=str)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def canonical_json(obj: Any) -> str:
    """Deterministic JSON string (sorted keys, no whitespace) — safe to hash."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def md5_hex(text: str) -> str:
    # Change detection, not security. usedforsecurity=False keeps this working
    # on FIPS-enabled systems (Python 3.9+).
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
