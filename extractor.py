"""
extractor.py

Step 1 of the ETL: pull raw data for every chart in the metadata catalog.

Reuses the existing abstractions — chart_service.list_charts() for the
catalog and chart_service.get_chart_data() for the fetch — so guest-token
handling, rate limiting and retry/backoff stay in dwh_client.py.

Design goals:
  * One bad chart never stops the run. Every exception is classified
    (timeout / empty_query / http_422 / auth / ...), logged, and recorded
    in the report.
  * Known-bad slice ids are skipped up front. The two timeouts alone would
    otherwise burn ~75s each (4 attempts x 15s + backoff) on every run.
  * Carry-forward: if a chart succeeded on a previous run but fails now,
    its previous data is reused (flagged stale=True). Without this, one
    transient failure would change the delta hash and drop that chart from
    the vector DB.
  * Two guards against a systemically broken run (expired cookies, portal
    down) so it can't overwrite good data with a mostly-empty dump.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import requests

import chart_service
from etl_common import STAGING_PATH, read_json

log = logging.getLogger("etl.extractor")

# --------------------------------------------------------------------------- #
# Known-bad slices (from your scraping tests)
# --------------------------------------------------------------------------- #

SKIP_TIMEOUT_IDS = {1038, 1115}                              # UMKM: heavy aggregation, times out even at 90s
SKIP_EMPTY_QUERY_IDS = {667, 1390, 1393, 1398, 1399, 1459}   # markdown/text blocks: 400 "Empty query?"
DEFAULT_SKIP_IDS = frozenset(SKIP_TIMEOUT_IDS | SKIP_EMPTY_QUERY_IDS)
# Deliberately NOT skipped: 1468/1469 (Simpeg, 422) and 1454 (Harga Pangan,
# auth). Those fail fast (4xx isn't retried), and they may start working once
# a referer/cookie is fixed — skipping them would hide that.

# --- systemic-failure guards ---
SYSTEMIC_CATEGORIES = {"auth", "connection", "timeout"}
SYSTEMIC_DASHBOARD_LIMIT = 3       # consecutive systemic failures spanning this many dashboards => abort
DEFAULT_MIN_SUCCESS_RATIO = 0.5    # of attempted (non-skipped) charts


class ExtractionAborted(RuntimeError):
    """The run looks systemically broken; nothing should be overwritten."""

    def __init__(self, message: str, report: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.report = report


class _EmptyRows(Exception):
    """Chart fetched fine but returned zero rows."""


@dataclass
class ExtractionResult:
    charts: Dict[int, Dict[str, Any]]   # slice_id -> chart record (fresh or stale)
    report: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #

_HTTP_STATUS_RE = re.compile(r"\b([45]\d{2})\b[^\n]{0,30}?(?:Client|Server) Error")


def classify_error(exc: BaseException) -> str:
    """
    Map an exception from chart_service.get_chart_data() to a short category.

    dwh_client wraps HTTP errors in RuntimeError (status code lives in the
    message), while transport-level failures (timeouts, connection resets)
    propagate as requests exceptions.
    """
    if isinstance(exc, _EmptyRows):
        return "empty_rows"
    if isinstance(exc, requests.exceptions.Timeout):        # check before ConnectionError
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection"
    if isinstance(exc, requests.exceptions.RequestException):
        return "request_error"

    msg = str(exc)
    if isinstance(exc, ValueError) and "not found in charts_metadata" in msg:
        return "not_in_catalog"
    if "Empty query" in msg:
        return "empty_query"
    if "no stored query_context" in msg:
        return "no_query_context"
    if "Could not authenticate" in msg or "Re-authentication failed" in msg:
        return "auth"
    match = _HTTP_STATUS_RE.search(msg)
    if match:
        code = match.group(1)
        return "auth" if code in ("401", "403") else f"http_{code}"
    if "no 'result' entries" in msg:
        return "no_result"
    return "unexpected"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_previous_charts(path=STAGING_PATH) -> Dict[int, Dict[str, Any]]:
    """Load the last staging dump as {slice_id: record} (empty if missing/corrupt)."""
    raw = read_json(path, default={}) or {}
    charts = raw.get("charts", {}) if isinstance(raw, dict) else {}
    out: Dict[int, Dict[str, Any]] = {}
    for key, record in charts.items():
        try:
            out[int(key)] = record
        except (TypeError, ValueError):
            continue
    return out


def _carry_forward(
    previous: Dict[int, Dict[str, Any]], entry: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    old = previous.get(entry["slice_id"])
    if not old or not old.get("rows"):
        return None
    return {
        **old,
        # Names come from the current catalog so renames still propagate.
        "slice_name": entry["slice_name"],
        "dashboard_name": entry.get("dashboard_name") or old.get("dashboard_name"),
        "stale": True,
    }


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def extract_all(
    previous: Optional[Dict[int, Dict[str, Any]]] = None,
    skip_ids: frozenset = DEFAULT_SKIP_IDS,
    min_success_ratio: float = DEFAULT_MIN_SUCCESS_RATIO,
) -> ExtractionResult:
    previous = previous or {}
    catalog = chart_service.list_charts()
    if not catalog:
        raise ExtractionAborted("charts_metadata is empty — run dwh_ingest.py first.")

    total = len(catalog)
    started_at = _now()
    t_start = time.monotonic()

    charts: Dict[int, Dict[str, Any]] = {}
    failures: List[Dict[str, Any]] = []
    skipped: List[int] = []
    carried: List[int] = []
    attempted = 0
    fresh_ok = 0
    streak_dashboards: Set[str] = set()

    def build_report(status: str) -> Dict[str, Any]:
        by_category: Dict[str, int] = {}
        for f in failures:
            by_category[f["category"]] = by_category.get(f["category"], 0) + 1
        return {
            "status": status,
            "started_at": started_at,
            "duration_s": round(time.monotonic() - t_start, 1),
            "catalog_size": total,
            "attempted": attempted,
            "succeeded": fresh_ok,
            "failed": len(failures),
            "skipped_known_bad": sorted(skipped),
            "carried_forward": sorted(carried),
            "failures_by_category": by_category,
            "failures": failures,
        }

    for i, entry in enumerate(catalog, start=1):
        slice_id = entry["slice_id"]
        slice_name = entry["slice_name"]
        dashboard = entry.get("dashboard_name") or "?"
        label = f"[{i}/{total}] slice {slice_id} '{slice_name}' ({dashboard})"

        if slice_id in skip_ids:
            log.info("%s: skipped (known-bad)", label)
            skipped.append(slice_id)
            fallback = _carry_forward(previous, entry)
            if fallback:
                charts[slice_id] = fallback
                carried.append(slice_id)
            continue

        attempted += 1
        t0 = time.monotonic()
        try:
            data = chart_service.get_chart_data(slice_id)
            if not data.get("rows"):
                raise _EmptyRows(f"slice {slice_id} returned 0 rows")
        except Exception as exc:  # noqa: BLE001 — the whole point is to survive anything
            category = classify_error(exc)
            elapsed = time.monotonic() - t0
            message = str(exc)[:300]
            log.warning("%s: FAILED [%s] after %.1fs — %s", label, category, elapsed, message)
            failures.append(
                {
                    "slice_id": slice_id,
                    "slice_name": slice_name,
                    "dashboard_name": dashboard,
                    "category": category,
                    "message": message,
                }
            )

            fallback = _carry_forward(previous, entry)
            if fallback:
                charts[slice_id] = fallback
                carried.append(slice_id)
                log.info("%s: reusing previous data (stale)", label)

            if category in SYSTEMIC_CATEGORIES:
                streak_dashboards.add(dashboard)
                if len(streak_dashboards) >= SYSTEMIC_DASHBOARD_LIMIT:
                    raise ExtractionAborted(
                        f"Consecutive auth/connection/timeout failures across "
                        f"{len(streak_dashboards)} dashboards — looks systemic "
                        "(expired cookies? portal down?). Aborting without touching staging.",
                        report=build_report("aborted_systemic"),
                    )
            else:
                streak_dashboards.clear()
            continue

        charts[slice_id] = {
            "slice_id": slice_id,
            "slice_name": slice_name,
            "dashboard_uuid": data.get("dashboard_uuid"),
            "dashboard_name": dashboard,
            "colnames": data.get("colnames", []),
            "rows": data["rows"],
            "fetched_at": _now(),
            "stale": False,
        }
        fresh_ok += 1
        streak_dashboards.clear()
        log.info("%s: OK — %d rows in %.1fs", label, len(data["rows"]), time.monotonic() - t0)

    if attempted and fresh_ok / attempted < min_success_ratio:
        raise ExtractionAborted(
            f"Only {fresh_ok}/{attempted} charts fetched successfully "
            f"(< {min_success_ratio:.0%}); refusing to overwrite staging data.",
            report=build_report("aborted_low_success"),
        )

    report = build_report("ok")
    log.info(
        "Extraction done: %d ok, %d failed, %d skipped, %d carried forward (%.0fs)",
        fresh_ok, len(failures), len(skipped), len(carried), report["duration_s"],
    )
    return ExtractionResult(charts=charts, report=report)
