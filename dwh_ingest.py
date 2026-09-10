"""
dwh_ingest.py

Scheduled job (cron / Airflow) that refreshes the chatbot's metadata
store: fetches chart metadata for every dashboard listed in
dashboards.py and upserts it into a local SQLite database, one row per
chart, tagged with which dashboard it came from.

Usage:
    python dwh_ingest.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from dashboards import DASHBOARDS, known_dashboards, pending_dashboards
from dwh_client import DWHClient, check_environment

DB_PATH = "chatbot_metadata.db"


# --------------------------------------------------------------------------- #
# 1. Fetcher — loops over every configured dashboard
# --------------------------------------------------------------------------- #

def fetch_all_charts(client: DWHClient) -> List[Dict[str, Any]]:
    """
    Fetch chart metadata for every dashboard that has a known numeric_id.

    Returns a flat list of dicts:
        {"slice_id", "slice_name", "dashboard_uuid", "dashboard_name"}

    A failure on one dashboard is logged and skipped rather than aborting
    the whole run — one broken/renamed dashboard shouldn't block the rest.
    """
    all_charts: List[Dict[str, Any]] = []
    ready = known_dashboards()
    pending = pending_dashboards()

    if pending:
        print(f"[fetch] Skipping {len(pending)} dashboard(s) with no numeric_id yet:")
        for d in pending:
            print(f"[fetch]   - {d['name']} ({d['uuid'][:8]}...)")

    for dashboard in ready:
        name, uuid = dashboard["name"], dashboard["uuid"]
        print(f"\n[fetch] --- {name} (id={dashboard['numeric_id']}) ---")
        try:
            charts = client.get_dashboard_charts(uuid)
        except Exception as e:
            print(f"[fetch][warn] Skipping '{name}': {e}")
            continue

        for chart in charts:
            chart["dashboard_uuid"] = uuid
            chart["dashboard_name"] = name
        all_charts.extend(charts)
        print(f"[fetch] '{name}': {len(charts)} chart(s)")

    print(f"\n[fetch] Total: {len(all_charts)} chart(s) across {len(ready)}/{len(DASHBOARDS)} dashboard(s)")
    return all_charts


# --------------------------------------------------------------------------- #
# 2. Database Setup (with migration for the new dashboard columns)
# --------------------------------------------------------------------------- #

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def init_db(conn: sqlite3.Connection) -> None:
    print("[db] Ensuring charts_metadata table exists")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS charts_metadata (
            slice_id       INTEGER PRIMARY KEY,
            slice_name     TEXT NOT NULL,
            dashboard_uuid TEXT,
            dashboard_name TEXT,
            last_updated   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Migrate older databases (from before dashboard_uuid/dashboard_name
    # existed) by adding any missing columns in place.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(charts_metadata)")}
    for col, decl in (("dashboard_uuid", "TEXT"), ("dashboard_name", "TEXT")):
        if col not in existing_cols:
            print(f"[db] Migrating: adding missing column '{col}'")
            conn.execute(f"ALTER TABLE charts_metadata ADD COLUMN {col} {decl}")

    conn.commit()


# --------------------------------------------------------------------------- #
# 3. Upsert Logic
# --------------------------------------------------------------------------- #

def upsert_charts_metadata(conn: sqlite3.Connection, charts: List[Dict[str, Any]]) -> None:
    if not charts:
        print("[db] No charts to upsert, skipping")
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (chart["slice_id"], chart["slice_name"], chart["dashboard_uuid"], chart["dashboard_name"], now)
        for chart in charts
    ]

    print(f"[db] Upserting {len(rows)} rows into charts_metadata")
    conn.executemany(
        """
        INSERT INTO charts_metadata
            (slice_id, slice_name, dashboard_uuid, dashboard_name, last_updated)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slice_id) DO UPDATE SET
            slice_name     = excluded.slice_name,
            dashboard_uuid = excluded.dashboard_uuid,
            dashboard_name = excluded.dashboard_name,
            last_updated   = excluded.last_updated
        """,
        rows,
    )
    conn.commit()
    print("[db] Upsert complete")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_ingestion(db_path: str = DB_PATH) -> None:
    if not check_environment():
        print("Aborting: fix the issue above and re-run.")
        sys.exit(1)

    known_ids = {d["uuid"]: d["numeric_id"] for d in known_dashboards()}
    known_referers = {d["uuid"]: d["referer"] for d in DASHBOARDS if d["referer"]}
    client = DWHClient(known_dashboard_ids=known_ids, known_referers=known_referers)
    client.load_base_cookies()

    charts = fetch_all_charts(client)

    conn = get_connection(db_path)
    try:
        init_db(conn)
        upsert_charts_metadata(conn, charts)
    finally:
        conn.close()
        print("[db] Connection closed")


if __name__ == "__main__":
    run_ingestion()
