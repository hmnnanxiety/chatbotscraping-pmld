"""
chart_service.py

High-level service layer for the live chatbot backend.
  - list_charts(): the full catalog (slice_id, slice_name, dashboard) from
    SQLite, kept fresh by dwh_ingest.py.
  - get_chart_data(slice_id): looks up which dashboard that chart belongs
    to, then fetches its live data via DWHClient using that dashboard's
    own guest token.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from dashboards import known_dashboards, DASHBOARDS
from dwh_client import DWHClient

DB_PATH = "chatbot_metadata.db"

_client: Optional[DWHClient] = None


def get_client() -> DWHClient:
    global _client
    if _client is None:
        known_ids = {d["uuid"]: d["numeric_id"] for d in known_dashboards()}
        known_referers = {d["uuid"]: d["referer"] for d in DASHBOARDS if d["referer"]}
        _client = DWHClient(known_dashboard_ids=known_ids, known_referers=known_referers)
        _client.load_base_cookies()
    return _client


def list_charts(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Return the full catalog: [{"slice_id", "slice_name", "dashboard_name"}, ...]."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT slice_id, slice_name, dashboard_name
            FROM charts_metadata
            ORDER BY dashboard_name, slice_name
            """
        )
        return [
            {"slice_id": row[0], "slice_name": row[1], "dashboard_name": row[2]}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def _lookup_dashboard_uuid(slice_id: int, db_path: str = DB_PATH) -> Optional[str]:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT dashboard_uuid FROM charts_metadata WHERE slice_id = ?",
            (slice_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_chart_data(slice_id: int) -> Dict[str, Any]:
    """Fetch actual data rows for a chart, resolving its dashboard automatically."""
    dashboard_uuid = _lookup_dashboard_uuid(slice_id)
    if not dashboard_uuid:
        raise ValueError(
            f"slice_id {slice_id} not found in charts_metadata. "
            "Call list_charts() first to get a valid slice_id."
        )
    return get_client().get_chart_data(dashboard_uuid, slice_id)
