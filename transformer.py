"""
transformer.py

Step 3 of the ETL: turn the raw staging dump into embedding-ready documents.

Reads staging_superset_data.json (not the in-memory extraction), so it can
also be run standalone to re-chunk without re-fetching from Superset:

    python transformer.py

Chunking strategy
-----------------
Each chart becomes one or more chunks, split by a character budget rather
than a fixed row count (rows vary a lot in width). Every chunk repeats a
small header — dashboard, chart, columns, row range — so it is
self-contained: a chunk retrieved on its own still says what the numbers
mean. Rows are rendered as "col: value | col: value" lines, which embeds
and reads better than raw JSON.

Output: a list of {"id", "page_content", "metadata"}. Metadata holds scalars
only (no lists), which every common vector DB accepts. The id is
deterministic (slice id + chunk index) so re-loading upserts instead of
duplicating.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from etl_common import READY_PATH, STAGING_PATH, atomic_write_json, md5_hex, read_json

log = logging.getLogger("etl.transformer")

MAX_CHUNK_CHARS = 1500


# --------------------------------------------------------------------------- #
# Value / row formatting
# --------------------------------------------------------------------------- #

def _format_value(col: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "N/A"
        if value.is_integer():
            return str(int(value))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if (
        col == "__timestamp"
        and isinstance(value, int)
        and abs(value) > 10**11                      # epoch milliseconds
    ):
        dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _format_row(row: Any, colnames: List[str]) -> str:
    if isinstance(row, (list, tuple)):
        row = dict(zip(colnames, row))
    if not isinstance(row, dict):
        return ""
    parts = [
        f"{col}: {_format_value(col, row[col])}"
        for col in (colnames or list(row))
        if row.get(col) is not None
    ]
    return ("- " + " | ".join(parts)) if parts else ""


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def chunk_chart(chart: Dict[str, Any], max_chars: int = MAX_CHUNK_CHARS) -> List[Dict[str, Any]]:
    rows = chart.get("rows") or []
    colnames = chart.get("colnames") or (list(rows[0]) if rows and isinstance(rows[0], dict) else [])
    dashboard = chart.get("dashboard_name") or "Unknown dashboard"
    chart_name = chart.get("slice_name") or f"Chart {chart.get('slice_id')}"

    # (original 1-based row number, rendered line); drop rows with no values at all
    lines = [(i, _format_row(r, colnames)) for i, r in enumerate(rows, start=1)]
    lines = [(i, text) for i, text in lines if text]
    if not lines:
        return []

    header_base = [f"Dashboard: {dashboard}", f"Chart: {chart_name}", f"Columns: {', '.join(colnames)}"]
    budget = max(200, max_chars - sum(len(h) + 1 for h in header_base) - 40)

    groups: List[List[Any]] = []
    current: List[Any] = []
    size = 0
    for item in lines:
        line_len = len(item[1]) + 1
        if current and size + line_len > budget:
            groups.append(current)
            current, size = [], 0
        current.append(item)
        size += line_len
    if current:
        groups.append(current)

    total_rows = len(rows)
    docs: List[Dict[str, Any]] = []
    for idx, group in enumerate(groups):
        first, last = group[0][0], group[-1][0]
        header = list(header_base)
        if len(groups) > 1:
            header.append(f"Rows {first}-{last} of {total_rows}")
        content = "\n".join(header + [""] + [text for _, text in group])

        docs.append(
            {
                "id": f"slice{chart['slice_id']}-chunk{idx}",
                "page_content": content,
                "metadata": {
                    "dashboard_name": dashboard,
                    "dashboard_uuid": chart.get("dashboard_uuid") or "",
                    "slice_id": chart["slice_id"],
                    "slice_name": chart_name,
                    "chunk_index": idx,
                    "chunk_count": len(groups),
                    "row_start": first,
                    "row_end": last,
                    "total_rows": total_rows,
                    "columns": ", ".join(colnames),
                    "content_hash": md5_hex(content),
                    "extracted_at": chart.get("fetched_at") or "",
                    "stale": bool(chart.get("stale", False)),
                },
            }
        )
    return docs


def transform(
    staging_path: Path = STAGING_PATH,
    out_path: Path = READY_PATH,
    max_chars: int = MAX_CHUNK_CHARS,
) -> List[Dict[str, Any]]:
    raw = read_json(staging_path)
    if not isinstance(raw, dict) or not raw.get("charts"):
        raise ValueError(f"{staging_path} is missing or has no charts — run the extractor first.")

    documents: List[Dict[str, Any]] = []
    for key in sorted(raw["charts"], key=int):
        chart_docs = chunk_chart(raw["charts"][key], max_chars)
        if not chart_docs:
            log.warning("slice %s produced no chunks (no usable rows)", key)
        documents.extend(chart_docs)

    atomic_write_json(out_path, documents, indent=1)
    avg = sum(len(d["page_content"]) for d in documents) / max(1, len(documents))
    log.info(
        "Transform done: %d chart(s) -> %d document(s), avg %.0f chars -> %s",
        len(raw["charts"]), len(documents), avg, out_path,
    )
    return documents


if __name__ == "__main__":
    from etl_common import setup_logging

    setup_logging()
    transform()
