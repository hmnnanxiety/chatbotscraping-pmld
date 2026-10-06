"""Superset extraction with dashboard-scoped authentication and effective filters."""
from __future__ import annotations

import copy
import json
import re
from html.parser import HTMLParser

from contracts import SourceTarget, UnifiedRecord, metadata_for
from dwh_client import DWHClient, SUPERSET_DOMAIN
from extractors.base import tls_verify

# Observed source-specific problem charts; override with --include-known-bad.
KNOWN_BAD = {1038: "known timeout", 1115: "known timeout",
             **{sid: "known empty query/text block" for sid in (667, 1390, 1393, 1398, 1399, 1459)}}


def as_json(value, default=None):
    return json.loads(value) if isinstance(value, str) and value else (value or default)


class BootstrapParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.dashboard_id = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and attrs.get("id") == "app" and attrs.get("data-bootstrap"):
            self.dashboard_id = json.loads(attrs["data-bootstrap"]).get("embedded", {}).get("dashboard_id")


def filter_applies(config, chart_id, layout):
    scope = config.get("scope") or {}
    if str(chart_id) in {str(x) for x in scope.get("excluded", [])}:
        return False
    if "chartsInScope" in config:
        return str(chart_id) in {str(x) for x in config["chartsInScope"]}
    roots = scope.get("rootPath", ["ROOT_ID"])
    if not roots or roots[-1] == "ROOT_ID":
        return True
    root = roots[-1]
    if root not in layout:
        raise ValueError(f"Cannot resolve native-filter scope {root}")
    visited, pending = set(), [root]
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        node = layout.get(key, {})
        if str(node.get("meta", {}).get("chartId")) == str(chart_id):
            return True
        pending.extend(node.get("children", []))
    return False


def effective_query(stored, dashboard, chart_id):
    """Mirror explicit defaultDataMask values, preserving chart-level constraints."""
    body = copy.deepcopy(as_json(stored))
    if not isinstance(body, dict) or not isinstance(body.get("queries"), list) or not body["queries"]:
        raise ValueError("Chart has no usable stored query_context")
    config = as_json(dashboard.get("json_metadata"), {})
    layout = as_json(dashboard.get("position_json"), {})
    effective = {}
    for nf in config.get("native_filter_configuration", []):
        if not filter_applies(nf, chart_id, layout):
            continue
        extra = (nf.get("defaultDataMask") or {}).get("extraFormData") or {}
        if not extra and nf.get("controlValues", {}).get("defaultToFirstItem"):
            raise ValueError("Dynamic first-item default requires explicit source configuration")
        unknown = set(extra) - {"filters", "time_range", "granularity_sqla", "time_grain_sqla"}
        if unknown:
            raise ValueError(f"Unsupported native filter fields: {sorted(unknown)}")
        for key, value in extra.items():
            if key == "filters":
                effective.setdefault(key, []).extend(copy.deepcopy(value))
            else:
                if key in effective and effective[key] != value:
                    raise ValueError(f"Conflicting default native filters: {key}")
                effective[key] = copy.deepcopy(value)
    for query in body["queries"]:
        filters = query.setdefault("filters", [])
        for item in effective.get("filters", []):
            if item not in filters:
                filters.append(copy.deepcopy(item))
        for key, value in effective.items():
            if key == "filters":
                continue
            if key == "time_grain_sqla":
                query.setdefault("extras", {})["time_grain_sqla"] = value
            else:
                query["granularity" if key == "granularity_sqla" else key] = value
    form = body.setdefault("form_data", {})
    form["slice_id"], form["dashboard_id"] = chart_id, dashboard["id"]
    form["extra_form_data"] = effective
    body["force"] = False
    return body


class SupersetAdapter:
    source_type = "superset"

    def __init__(self, dashboard, client=None):
        self.config = dashboard
        self.adapter_id = f"superset:{dashboard['uuid']}"
        self.client = client or DWHClient(
            known_dashboard_ids=({dashboard["uuid"]: dashboard["numeric_id"]} if dashboard.get("numeric_id") else {}),
            known_referers=({dashboard["uuid"]: dashboard["referer"]} if dashboard.get("referer") else {}),
            request_retries=1, verify=tls_verify(),
        )
        self.dashboard = None

    def discover(self):
        uuid = self.config["uuid"]
        self.client.load_base_cookies()
        numeric_id = self.config.get("numeric_id")
        if numeric_id is None:
            response = self.client._authed_get(
                f"{SUPERSET_DOMAIN}/embedded/{uuid}", uuid, context="resolve-dashboard",
            )
            parser = BootstrapParser()
            parser.feed(response.text)
            numeric_id = parser.dashboard_id
            if not isinstance(numeric_id, int):
                raise ValueError("embedded.dashboard_id missing in bootstrap JSON")
            self.client._dashboard_info[uuid] = {"id": numeric_id, "title": self.config["name"]}
        self.dashboard = self.client._authed_get(
            f"{SUPERSET_DOMAIN}/api/v1/dashboard/{numeric_id}", uuid, context="dashboard-config",
        ).json()["result"]
        self.dashboard["id"] = numeric_id
        return [SourceTarget(
            self.source_type, str(numeric_id), str(c["slice_id"]),
            f"{SUPERSET_DOMAIN}/embedded/{uuid}", c["slice_name"],
            {"scope_id": uuid, "dashboard_name": self.config["name"],
             "source_name": "IDMC DIY / Superset", "skip_reason": KNOWN_BAD.get(c["slice_id"])},
        ) for c in self.client.get_dashboard_charts(uuid)]

    def extract(self, target):
        if self.dashboard is None:
            raise ValueError("Call discover before extract")
        uuid, sid = self.config["uuid"], int(target.chart_id)
        meta = self.client._authed_get(
            f"{SUPERSET_DOMAIN}/api/v1/chart/{sid}", uuid, context="chart-metadata",
        ).json()["result"]
        body = effective_query(meta.get("query_context"), self.dashboard, sid)
        response = self.client._authed_post(
            f"{SUPERSET_DOMAIN}/api/v1/chart/data", uuid, context="chart-data", json=body,
        ).json()
        results = response.get("result")
        if not isinstance(results, list) or len(results) != len(body["queries"]):
            raise ValueError("Missing query results; refusing to publish incomplete chart")
        layout = as_json(self.dashboard.get("position_json"), {})
        notes = "\n".join(
            node.get("meta", {}).get("code", "")
            for node in layout.values() if isinstance(node, dict) and node.get("type") == "MARKDOWN"
        )
        updated = re.search(r"Last Update\s*:\s*([^\n(]+)", notes, re.I)
        records = []
        for index, (result, query) in enumerate(zip(results, body["queries"])):
            if result.get("error") or result.get("status") in {"failed", "error", "pending", "running"}:
                raise ValueError(f"Chart result {index} did not complete")
            rows, cols = result.get("data"), result.get("colnames", [])
            if len(set(cols)) != len(cols):
                raise ValueError("Duplicate result columns would lose values")
            if not isinstance(rows, list):
                raise ValueError("Chart result data is not a list")
            normalized = []
            for row in rows:
                if isinstance(row, (list, tuple)):
                    if len(row) != len(cols):
                        raise ValueError("Chart row width differs from columns")
                    row = dict(zip(cols, row))
                if not isinstance(row, dict):
                    raise ValueError("Unsupported chart row shape")
                normalized.append(row)
            period_filters = [f for f in query.get("filters", [])
                              if str(f.get("col", "")).lower() in {"tahun", "year"}]
            period = {"filters": period_filters, "time_range": query.get("time_range")}
            params = as_json(meta.get("params"), {})
            records.append(UnifiedRecord(
                self.source_type, target.dashboard_id, target.chart_id, normalized,
                metadata_for(
                    target, columns=cols, statistical_period=period if any(period.values()) else None,
                    source_update_date=updated.group(1).strip() if updated else None,
                    unit=params.get("unit"), dashboard_uuid=uuid,
                    effective_filters=query.get("filters", []),
                    time_range=query.get("time_range"), metrics=query.get("metrics", []),
                    row_limit=query.get("row_limit"),
                    source_notes=re.sub(r"Last Update\s*:\s*[^\n(]+", "", notes, flags=re.I).strip(),
                    source_update_note=updated.group(0).strip() if updated else None,
                    is_complete=False if query.get("row_limit") and len(rows) >= query["row_limit"] else "unknown",
                    extraction_mode="api", source_url=f"{SUPERSET_DOMAIN}/explore/?slice_id={sid}",
                    portal_url="https://idmc.jogjaprov.go.id/",
                ), target.id, index,
            ))
        return records
