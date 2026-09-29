"""
delta_checker.py

Step 2 of the ETL: decide whether the extracted data differs from the last
run, using MD5 hashes.

Three details that matter for not getting false "changed" results:

  1. Only *content* is hashed (names, columns, rows). Volatile fields such
     as fetched_at / stale are excluded — otherwise every run would differ.
  2. Rows are sorted before hashing. Superset gives no ordering guarantee
     for queries without an ORDER BY, and a reshuffle isn't a data change.
  3. The new hash is only persisted by commit(), which the orchestrator
     calls AFTER transform (and vector-DB load, if enabled) succeeds. If
     anything fails midway, the next run sees "changed" again and retries.

The overall hash is the MD5 of the per-chart hashes, so it also tells you
*which* charts changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from etl_common import (
    READY_PATH,
    STATE_PATH,
    TRANSFORM_VERSION,
    atomic_write_json,
    canonical_json,
    md5_hex,
    read_json,
)

log = logging.getLogger("etl.delta")


def chart_fingerprint(chart: Dict[str, Any]) -> str:
    payload = {
        "slice_name": chart.get("slice_name"),
        "dashboard_name": chart.get("dashboard_name"),
        "colnames": chart.get("colnames", []),
        "rows": sorted(canonical_json(row) for row in chart.get("rows", [])),
    }
    return md5_hex(canonical_json(payload))


@dataclass
class DeltaResult:
    changed: bool
    reason: str
    new_hash: str
    chart_hashes: Dict[str, str]
    changed_slice_ids: List[int] = field(default_factory=list)


class DeltaChecker:
    def __init__(self, state_path: Path = STATE_PATH, ready_path: Path = READY_PATH) -> None:
        self.state_path = Path(state_path)
        self.ready_path = Path(ready_path)

    def check(self, charts: Dict[int, Dict[str, Any]], force: bool = False) -> DeltaResult:
        chart_hashes = {str(sid): chart_fingerprint(c) for sid, c in sorted(charts.items())}
        new_hash = md5_hex(canonical_json(chart_hashes))

        state = read_json(self.state_path, default=None)
        old_hashes: Dict[str, str] = (state or {}).get("chart_hashes", {})
        changed_ids = sorted(
            int(k)
            for k in set(chart_hashes) | set(old_hashes)
            if chart_hashes.get(k) != old_hashes.get(k)
        )

        if force:
            reason = "forced"
        elif state is None:
            reason = "first_run"
        elif state.get("transform_version") != TRANSFORM_VERSION:
            reason = "transform_version_changed"
        elif not self.ready_path.exists():
            reason = "output_missing"
        elif state.get("hash") != new_hash:
            reason = "data_changed"
        else:
            reason = "unchanged"

        result = DeltaResult(
            changed=reason != "unchanged",
            reason=reason,
            new_hash=new_hash,
            chart_hashes=chart_hashes,
            changed_slice_ids=changed_ids,
        )
        log.info(
            "Delta check: %s (hash %s, previous %s, %d chart(s) differ)",
            reason, new_hash[:10], (state or {}).get("hash", "none")[:10], len(changed_ids),
        )
        return result

    def commit(self, result: DeltaResult) -> None:
        atomic_write_json(
            self.state_path,
            {
                "hash": result.new_hash,
                "chart_hashes": result.chart_hashes,
                "chart_count": len(result.chart_hashes),
                "transform_version": TRANSFORM_VERSION,
                "committed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        log.info("Delta state committed (%s)", result.new_hash[:10])
