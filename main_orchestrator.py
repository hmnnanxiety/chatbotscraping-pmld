"""
main_orchestrator.py

Runs the ETL sequentially:

    1. (optional) refresh chart metadata      dwh_ingest.py
    2. Extract   every chart                  extractor.py
    3. Delta check (MD5)                      delta_checker.py   -> halt if unchanged
    4. Dump raw data to staging JSON          staging_superset_data.json
    5. Transform staging -> chunks            transformer.py     -> ready_for_vector_db.json
    6. (optional) load chunks into vector DB  vector_store.py
    7. Commit the new hash                    only now, so a failure in 4-6 is retried next run

Usage (schedule off-peak, e.g. cron `0 2 * * *`):
    python main_orchestrator.py
    python main_orchestrator.py --force                 # ignore the delta check
    python main_orchestrator.py --refresh-metadata      # run dwh_ingest first
    python main_orchestrator.py --load-vector-db        # also embed + upsert
    python main_orchestrator.py --include-known-bad     # retry the known-bad slice ids

Exit codes: 0 = success or nothing changed, 1 = failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from delta_checker import DeltaChecker
from dwh_client import check_environment
from etl_common import REPORT_PATH, STAGING_PATH, atomic_write_json, setup_logging
from extractor import (
    DEFAULT_MIN_SUCCESS_RATIO,
    DEFAULT_SKIP_IDS,
    ExtractionAborted,
    extract_all,
    load_previous_charts,
)
from transformer import transform

log = logging.getLogger("etl.main")


def run(
    force: bool = False,
    refresh_metadata: bool = False,
    include_known_bad: bool = False,
    load_vector_db: bool = False,
    min_success_ratio: float = DEFAULT_MIN_SUCCESS_RATIO,
) -> int:
    if not check_environment():
        return 1

    # 1. Optional metadata refresh -------------------------------------------
    if refresh_metadata:
        import dwh_ingest
        log.info("Refreshing chart metadata (dwh_ingest)")
        try:
            dwh_ingest.run_ingestion()
        except SystemExit as e:  # run_ingestion() calls sys.exit(1) on env problems
            log.error("Metadata refresh failed (exit %s)", e.code)
            return 1

    # 2. Extract --------------------------------------------------------------
    previous = load_previous_charts(STAGING_PATH)
    skip_ids = frozenset() if include_known_bad else DEFAULT_SKIP_IDS
    try:
        extraction = extract_all(previous, skip_ids=skip_ids, min_success_ratio=min_success_ratio)
    except ExtractionAborted as e:
        log.error("Extraction aborted: %s", e)
        if e.report:
            atomic_write_json(REPORT_PATH, e.report)
        return 1
    atomic_write_json(REPORT_PATH, extraction.report)

    # 3. Delta check ----------------------------------------------------------
    checker = DeltaChecker()
    delta = checker.check(extraction.charts, force=force)
    if not delta.changed:
        log.info("No changes since the last run — halting pipeline.")
        return 0
    log.info("Proceeding (%s); changed slice ids: %s", delta.reason, delta.changed_slice_ids or "n/a")

    try:
        # 4. Staging dump (raw backup layer) ---------------------------------
        atomic_write_json(
            STAGING_PATH,
            {
                "schema_version": 1,
                "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "chart_count": len(extraction.charts),
                "charts": {str(sid): chart for sid, chart in sorted(extraction.charts.items())},
            },
        )
        log.info("Staging dump written: %s (%d charts)", STAGING_PATH, len(extraction.charts))

        # 5. Transform: staging file -> chunks -------------------------------
        documents = transform(STAGING_PATH)

        # 6. Optional load ---------------------------------------------------
        if load_vector_db:
            import vector_store  # lazy: the ETL itself has no vector-DB dependency
            vector_store.index_documents(documents)
    except Exception:
        log.exception("Pipeline failed after extraction; hash NOT committed, next run will retry")
        return 1

    # 7. Commit ---------------------------------------------------------------
    checker.commit(delta)
    log.info("Pipeline complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DWH -> vector DB ETL")
    parser.add_argument("--force", action="store_true", help="skip the delta check")
    parser.add_argument("--refresh-metadata", action="store_true", help="run dwh_ingest first")
    parser.add_argument("--include-known-bad", action="store_true", help="don't skip known-bad slice ids")
    parser.add_argument("--load-vector-db", action="store_true", help="embed and upsert into the vector DB")
    parser.add_argument("--min-success-ratio", type=float, default=DEFAULT_MIN_SUCCESS_RATIO)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    return run(
        force=args.force,
        refresh_metadata=args.refresh_metadata,
        include_known_bad=args.include_known_bad,
        load_vector_db=args.load_vector_db,
        min_success_ratio=args.min_success_ratio,
    )


if __name__ == "__main__":
    sys.exit(main())
