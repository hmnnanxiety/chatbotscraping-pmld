"""Explicit migration of v1/experimental v2 snapshots, never silent recovery."""
import json
from pathlib import Path
from contracts import NormalizedRecord, NORMALIZATION_VERSION, SCHEMA_VERSION

def _upgrade(value, key=None):
    value = dict(value)
    meta = dict(value["metadata"])
    meta.setdefault("source_name", value["source_type"])
    meta.setdefault("chart_name", meta.get("slice_name") or str(value["chart_id"]))
    meta["dashboard_name"] = meta.get("dashboard_name") or str(value["dashboard_id"])
    meta.setdefault("effective_filters", [])
    for name in ("statistical_period", "unit", "source_update_date"):
        meta.setdefault(name, None)
    for name in ("last_checked_at", "last_successful_fetch_at"):
        meta[name] = meta.get(name) or None
    meta.setdefault("is_complete", "unknown")
    if meta["last_successful_fetch_at"] is None or meta["last_checked_at"] is None:
        meta["stale"] = True
    value["metadata"] = meta
    identity = f"{value['target_id']}:result{value.get('result_index', 0)}"
    value.setdefault("record_id", identity)
    if key is not None and key != identity:
        raise ValueError("Staging key does not match record identity")
    return NormalizedRecord.from_dict(value)

def load_staging(path, legacy_path=None):
    path = Path(path)
    if not path.exists():
        if legacy_path is None or not Path(legacy_path).exists():
            return {}
        path = Path(legacy_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") == SCHEMA_VERSION and "records" in raw:
        if raw.get("normalization_version") not in (None, NORMALIZATION_VERSION):
            raise ValueError("Unsupported normalization version")
        if not isinstance(raw["records"], dict):
            raise ValueError("staging.records must be an object")
        current = raw.get("normalization_version") == NORMALIZATION_VERSION
        result = {}
        for key, value in raw["records"].items():
            record = NormalizedRecord.from_dict(value) if current else _upgrade(value, key)
            if key != record.id:
                raise ValueError("Staging key does not match record identity")
            result[key] = record
        return result
    if "charts" not in raw or raw.get("schema_version", 1) != 1:
        raise ValueError("Unsupported staging schema/version")
    # Legacy chart->normalized conversion is migration only, not a generic ETL dependency.
    from dashboards import DASHBOARDS
    ids = {d["uuid"]: d["numeric_id"] for d in DASHBOARDS}
    result = {}
    for old in raw["charts"].values():
        uuid, sid = old["dashboard_uuid"], str(old["slice_id"])
        columns = old.get("colnames", [])
        rows = []
        for row in old["rows"]:
            if isinstance(row, list):
                if len(row) != len(columns) or len(set(columns)) != len(columns):
                    raise ValueError("Legacy row width/columns invalid")
                row = dict(zip(columns, row))
            rows.append(row)
        record = _upgrade({
            "source_type": "superset", "dashboard_id": str(ids.get(uuid) or uuid),
            "chart_id": sid, "raw_rows": rows, "target_id": f"superset:{uuid}:{sid}",
            "metadata": {
                "dashboard_name": old.get("dashboard_name"), "slice_name": old.get("slice_name"),
                "dashboard_uuid": uuid, "columns": columns,
                "source_url": f"https://dwh.jogjaprov.go.id/explore/?slice_id={sid}",
                "last_checked_at": old.get("fetched_at"),
                "last_successful_fetch_at": old.get("fetched_at"),
                "stale": True, "extraction_mode": "legacy_unverified",
                "last_error": "Legacy snapshot; effective filters not verified",
            },
        })
        if record.id in result:
            raise ValueError("Duplicate legacy record identity")
        result[record.id] = record
    return result

def staging_payload(records):
    return {
        "schema_version": SCHEMA_VERSION, "normalization_version": NORMALIZATION_VERSION,
        "records": {key: records[key].normalized().to_dict() for key in sorted(records)},
    }
