"""Source-neutral normalized contract. Normalization never reads the clock."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from etl_common import canonical_json, md5_hex

SCHEMA_VERSION = 2
NORMALIZATION_VERSION = "2.1.0"
FRESHNESS_FIELDS = {
    "last_checked_at", "last_successful_fetch_at", "source_update_date",
    "source_update_note", "fetched_at", "extracted_at", "stale",
    "last_error", "extraction_warning",
}
SEMANTIC_METADATA = {
    "source_name", "dashboard_name", "chart_name", "source_url", "columns",
    "statistical_period", "effective_filters", "unit", "extraction_mode",
    "is_complete", "time_range", "description", "title",
}
REQUIRED_METADATA = {
    "source_name", "dashboard_name", "chart_name", "source_url",
    "statistical_period", "effective_filters", "source_update_date", "unit",
    "last_checked_at", "last_successful_fetch_at", "stale", "is_complete", "extraction_mode",
}

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def timestamp(value, field_name, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must have a timezone")
    return parsed

@dataclass
class SourceTarget:
    source_type: str
    dashboard_id: str
    chart_id: str
    source_url: str
    name: str
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self):
        return f"{self.source_type}:{self.options.get('scope_id', self.dashboard_id)}:{self.chart_id}"

@dataclass
class NormalizedRecord:
    source_type: str
    dashboard_id: str
    chart_id: str
    raw_rows: list[dict[str, Any]]
    metadata: dict[str, Any]
    target_id: str
    result_index: int = 0

    @property
    def record_id(self):
        return f"{self.target_id}:result{self.result_index}"

    @property
    def id(self):
        return self.record_id

    def validate(self):
        for name in ("source_type", "dashboard_id", "chart_id", "target_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"Missing/invalid {name}")
        if type(self.result_index) is not int or self.result_index < 0:
            raise ValueError("result_index must be a nonnegative integer")
        if not isinstance(self.metadata, dict) or REQUIRED_METADATA - self.metadata.keys():
            raise ValueError("Missing/invalid required provenance metadata")
        if not isinstance(self.raw_rows, list) or any(not isinstance(r, dict) for r in self.raw_rows):
            raise ValueError("raw_rows must be a list of objects")
        for row in self.raw_rows:
            if any(not isinstance(key, str) for key in row):
                raise ValueError("Row column names must be strings")
        canonical_json(self.raw_rows)  # rejects NaN, infinity and non-JSON values
        canonical_json(self.metadata)
        m = self.metadata
        portal_fields = {"portal_menu_name", "portal_page_name", "portal_visible",
                         "source_dashboard_name", "source_chart_name"}
        if portal_fields.intersection(m):
            if portal_fields - m.keys() or type(m["portal_visible"]) is not bool:
                raise ValueError("Missing/invalid portal and source identity metadata")
            for name in portal_fields - {"portal_visible"}:
                if not isinstance(m[name], str) or not m[name].strip():
                    raise ValueError(f"Missing/invalid metadata.{name}")
        for name in ("source_name", "dashboard_name", "chart_name", "extraction_mode"):
            if not isinstance(m[name], str) or not m[name].strip():
                raise ValueError(f"Missing/invalid metadata.{name}")
        url = urlparse(m["source_url"]) if isinstance(m["source_url"], str) else None
        if not url or url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            raise ValueError("source_url must be an absolute public-source URL without credentials")
        if type(m["stale"]) is not bool or not (type(m["is_complete"]) is bool or m["is_complete"] == "unknown"):
            raise ValueError("stale/is_complete have invalid types")
        if not isinstance(m["effective_filters"], (list, dict)):
            raise ValueError("effective_filters must be a list or object")
        if m["unit"] is not None and not isinstance(m["unit"], str):
            raise ValueError("unit must be a string or null")
        if m["source_update_date"] is not None and not isinstance(m["source_update_date"], str):
            raise ValueError("source_update_date must be a source-reported string or null")
        checked = timestamp(m["last_checked_at"], "last_checked_at", nullable=True)
        fetched = timestamp(m["last_successful_fetch_at"], "last_successful_fetch_at", nullable=True)
        if not m["stale"] and (checked is None or fetched is None):
            raise ValueError("Fresh data requires checked and successful-fetch timestamps")
        if checked and fetched and fetched > checked:
            raise ValueError("Successful fetch cannot be later than last check")
        if m["extraction_mode"] == "metadata_only" and self.raw_rows:
            raise ValueError("metadata_only records must not contain numeric rows")
        return self

    def normalized(self):
        self.validate()
        raw = asdict(self)
        raw["raw_rows"] = sorted(self.raw_rows, key=canonical_json)
        # JSON round-trip provides stable key order, independent copies and types.
        import json
        return NormalizedRecord(**json.loads(canonical_json(raw)))

    def to_dict(self):
        return {"record_id": self.record_id, **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        identity = value.pop("record_id", None)
        record = cls(**value)
        if identity != record.record_id:
            raise ValueError("Missing or inconsistent record_id")
        return record.normalized()

    @property
    def content_hash(self):
        return md5_hex(canonical_json({
            "record_id": self.record_id, "target_id": self.target_id,
            "source_type": self.source_type, "dashboard_id": self.dashboard_id,
            "chart_id": self.chart_id, "rows": sorted(self.raw_rows, key=canonical_json),
            "metadata": {k: self.metadata.get(k) for k in sorted(
                SEMANTIC_METADATA | ({"portal_menu_name", "portal_page_name", "portal_visible",
                                      "source_dashboard_name", "source_chart_name"} & self.metadata.keys()))},
        }))

    @property
    def metadata_hash(self):
        return md5_hex(canonical_json(self.metadata))

# Alias used by adapters; the public serialized contract is NormalizedRecord.
UnifiedRecord = NormalizedRecord

def metadata_for(target, **extra):
    now = utcnow()
    return {
        "source_name": target.options.get("source_name", target.source_type),
        "dashboard_name": target.options.get("dashboard_name", target.name),
        "chart_name": target.name, "slice_name": target.name,
        "statistical_period": None, "source_update_date": None, "unit": None,
        "effective_filters": [], "source_url": target.source_url,
        "last_checked_at": now, "last_successful_fetch_at": now,
        "stale": False, "is_complete": "unknown", "extraction_mode": "api", **extra,
    }
