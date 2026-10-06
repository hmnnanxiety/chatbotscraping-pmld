"""Generic discovery/extraction, per-target isolation and publication safety gates."""
from __future__ import annotations
import copy
import logging
import time
from collections import Counter
from dataclasses import dataclass
import requests
from contracts import NormalizedRecord, utcnow
from extractors.base import retry_source
from ingestion_config import friendly_http_error

log = logging.getLogger("etl.extractor")
DEFAULT_MIN_SUCCESS_RATIO = 0.5
SYSTEMIC_DASHBOARD_LIMIT = 3
SYSTEMIC_CATEGORIES = {"auth", "connection", "timeout", "tls", "http_429", "http_500", "http_502", "http_503", "http_504"}

class ExtractionAborted(RuntimeError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report

@dataclass
class ExtractionResult:
    records: dict[str, NormalizedRecord]
    report: dict

def classify_error(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    response = getattr(exc, "response", None)
    if response is not None:
        code = response.status_code
        return "auth" if code in {401, 403} else f"http_{code}"
    if exc.__cause__ is not None:
        return classify_error(exc.__cause__)
    if "authenticate" in str(exc).lower() or "authentication" in str(exc).lower():
        return "auth"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "validation"
    return "unexpected"

def error_summary(exc):
    category = classify_error(exc)
    # Never include HTTP response bodies, cookies, tokens or URLs in runtime reports.
    detail = (str(exc)[:180] if category == "validation" else
              friendly_http_error(exc) if isinstance(exc, requests.RequestException) else
              "Authentication failed. Check the guest-token error above and refresh fallback cookies in .env."
              if category == "auth" else type(exc).__name__)
    return category, detail

@retry_source
def discover_adapter(adapter):
    return adapter.discover()

@retry_source
def execute_adapter(adapter, target):
    return adapter.extract(target)

def _owns(adapter, record):
    prefixes = getattr(adapter, "target_prefixes", None)
    if prefixes is None:
        prefixes = (getattr(adapter, "adapter_id", adapter.source_type) + ":",)
    return record.source_type == adapter.source_type and record.target_id.startswith(prefixes)

def extract_all(previous=None, *, adapters, min_success_ratio=DEFAULT_MIN_SUCCESS_RATIO,
                include_known_bad=False, now=None, record_validator=None):
    if not 0 <= min_success_ratio <= 1:
        raise ValueError("min_success_ratio must be between 0 and 1")
    previous = previous or {}
    checked = now or utcnow()
    started = time.monotonic()
    records = copy.deepcopy(previous)
    for record in records.values():
        record.metadata.update(stale=True, last_error="Not refreshed in this run")
    report = {
        "status": "ok", "started_at": checked, "targets_discovered": 0,
        "attempted": 0, "succeeded": 0, "failed": 0, "carried_forward": 0,
        "skipped": 0, "metadata_only": 0, "records_produced": 0, "chunks_produced": 0,
        "unavailable_targets": 0, "failures": [], "failures_by_category": {},
        "skipped_targets": [], "carried_forward_targets": [],
    }
    streak, carried, discovered = set(), set(), set()

    def finish(status):
        report.update(status=status, duration_s=round(time.monotonic() - started, 3),
                      carried_forward=len(carried), carried_forward_targets=sorted(carried),
                      records_produced=len(records),
                      failures_by_category=dict(sorted(Counter(f["category"] for f in report["failures"]).items())))
        return report

    def abort(reason):
        report["abort_reason"] = reason
        raise ExtractionAborted(reason, finish("aborted"))

    def carry(target_id, reason, target=None):
        for record in records.values():
            if record.target_id == target_id:
                # Keep the actual successful-fetch timestamp, including null for unknown legacy data.
                record.metadata.update(stale=True, last_checked_at=checked, last_error=reason)
                if target:
                    record.metadata["chart_name"] = record.metadata["slice_name"] = target.name
                    record.metadata["dashboard_name"] = target.options.get("dashboard_name", record.metadata["dashboard_name"])
                carried.add(target_id)

    def failure(adapter_id, target_id, stage, exc, scope):
        category, message = error_summary(exc)
        report["failures"].append({"adapter": adapter_id, "target_id": target_id,
                                  "stage": stage, "category": category, "message": message})
        log.warning("%s %s failed [%s]: %s", adapter_id, target_id or stage, category, message)
        if category in SYSTEMIC_CATEGORIES:
            streak.add(scope)
            if len(streak) >= SYSTEMIC_DASHBOARD_LIMIT:
                abort("Consecutive systemic failures across three source/dashboard scopes")
        else:
            streak.clear()

    adapter_count = len(adapters) if hasattr(adapters, "__len__") else "?"
    for adapter_number, adapter in enumerate(adapters, 1):
        adapter_id = getattr(adapter, "adapter_id", adapter.source_type)
        label = getattr(adapter, "config", {}).get("name", adapter_id)
        log.info("[%s/%s] %s: discovering targets...", adapter_number, adapter_count, label)
        owned = {r.target_id for r in records.values() if _owns(adapter, r)}
        try:
            targets = list(discover_adapter(adapter))
            ids = [t.id for t in targets]
            if len(set(ids)) != len(ids) or discovered.intersection(ids):
                raise ValueError("Duplicate target IDs during discovery")
            if not targets:
                raise ValueError("Discovery returned no targets")
        except Exception as exc:
            report["unavailable_targets"] += max(1, len(owned))
            for target_id in owned:
                carry(target_id, "Discovery failed")
            failure(adapter_id, None, "discovery", exc, adapter_id)
            continue
        discovered.update(ids)
        report["targets_discovered"] += len(targets)
        log.info("[%s/%s] %s: %d target(s)", adapter_number, adapter_count, label, len(targets))
        for missing in sorted(owned - set(ids)):
            carry(missing, "Previously known target absent from discovery")
            report["unavailable_targets"] += 1
            failure(adapter_id, missing, "discovery", ValueError("Previously known target absent from discovery"), adapter_id)
        for target_number, target in enumerate(sorted(targets, key=lambda t: t.id), 1):
            if target.options.get("skip_reason") and not include_known_bad:
                log.info("  [%d/%d] %s ... SKIPPED (%s)", target_number, len(targets),
                         target.name, target.options["skip_reason"])
                report["skipped"] += 1
                report["skipped_targets"].append({"target_id": target.id, "reason": target.options["skip_reason"]})
                carry(target.id, target.options["skip_reason"], target)
                continue
            report["attempted"] += 1
            log.info("  [%d/%d] %s ... extracting", target_number, len(targets), target.name)
            old = {key: r for key, r in records.items() if r.target_id == target.id}
            try:
                extracted = execute_adapter(adapter, target)
                if not isinstance(extracted, list) or not extracted:
                    raise ValueError("Adapter must return at least one normalized record (empty rows are allowed)")
                normalized = {}
                for record in extracted:
                    if not isinstance(record, NormalizedRecord) or record.target_id != target.id or record.source_type != target.source_type:
                        raise ValueError("Adapter returned invalid/mismatched record")
                    record = copy.deepcopy(record)
                    record.metadata.update(last_checked_at=checked, last_successful_fetch_at=checked, stale=False)
                    record.metadata.pop("last_error", None)
                    record = record.normalized()
                    if record.id in normalized:
                        raise ValueError("Duplicate normalized record IDs")
                    normalized[record.id] = record
                if any(r.raw_rows for r in old.values()) and all(r.metadata["extraction_mode"] == "metadata_only" for r in normalized.values()):
                    raise ValueError("Structured extraction degraded to metadata-only")
                if record_validator:
                    record_validator(normalized)
            except Exception as exc:
                report["failed"] += 1
                carry(target.id, classify_error(exc), target)
                scope = target.options.get("health_scope", f"{target.source_type}:{target.dashboard_id}")
                failure(adapter_id, target.id, "extract", exc, scope)
                continue
            for key in old:
                del records[key]
            records.update(normalized)
            log.info("  [%d/%d] %s ... OK (%d rows, %d records)", target_number, len(targets),
                     target.name, sum(len(r.raw_rows) for r in normalized.values()), len(normalized))
            report["succeeded"] += 1
            report["metadata_only"] += int(all(r.metadata["extraction_mode"] == "metadata_only" for r in normalized.values()))
            streak.clear()

    denominator = report["attempted"] + report["unavailable_targets"]
    ratio = report["succeeded"] / denominator if denominator else 0
    report["success_ratio"] = ratio
    if denominator and (not report["succeeded"] or ratio < min_success_ratio):
        abort(f"Success ratio {report['succeeded']}/{denominator} below publication guard ({min_success_ratio:.0%})")
    if not records or not discovered:
        abort("No usable discovered targets/records; refusing to overwrite staging")
    carried.update(r.target_id for r in records.values() if r.metadata["stale"])
    status = "partial" if report["failures"] or report["skipped"] or carried else "ok"
    return ExtractionResult(records, finish(status))
