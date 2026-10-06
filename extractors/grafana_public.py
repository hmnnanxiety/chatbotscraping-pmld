"""Read-only Grafana public-panel queries. Never execute renderer JS or private queries."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import math
import re
import time
from urllib.parse import urlsplit

import requests

from contracts import NormalizedRecord, SourceTarget, metadata_for, timestamp
from etl_common import canonical_json
from extractors.base import HTTPClient, origin


class GrafanaExtractionError(ValueError):
    def __init__(self, message, category="validation"):
        super().__init__(message)
        self.ingestion_category = category


def public_identity(url):
    origin(url)
    p = urlsplit(url)
    match = re.fullmatch(r"/public-dashboards/([0-9a-f]{32})/?", p.path)
    if not match or p.query or p.fragment:
        raise GrafanaExtractionError("Expected an unfiltered HTTPS public-dashboard URL")
    return f"https://{p.netloc.lower()}", match[1]


class GrafanaHTTPClient(HTTPClient):
    def query(self, url, payload, *, allowed_origin):
        if origin(url) != allowed_origin:
            raise GrafanaExtractionError("Cross-origin public query rejected")
        delay = self.min_interval - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()
        response = self.session.post(url, json=payload, timeout=self.timeout,
                                     verify=self.verify, allow_redirects=False,
                                     headers={"Accept": "application/json",
                                              "User-Agent": "IDMC-Snapshot-Ingestion/2.0"})
        # Do not replay a POST (or credentials) through redirects.
        if 300 <= response.status_code < 400:
            raise GrafanaExtractionError("Public panel query redirect rejected")
        response.raise_for_status()
        return response


def query_range(saved, anchor, timezone_name):
    """Small supported subset of Grafana date math; refuse unsupported rounding/macros."""
    if not isinstance(saved, dict):
        raise GrafanaExtractionError("Dashboard time range missing")
    def parse(value):
        if value == "now":
            return anchor
        m = re.fullmatch(r"now-(\d+)([smhdw])", str(value))
        if m:
            seconds = int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]
            return anchor - timedelta(seconds=seconds)
        return timestamp(value, "dashboard time")
    start, end = parse(saved.get("from")), parse(saved.get("to"))
    if start >= end:
        raise GrafanaExtractionError("Dashboard time range is not increasing")
    return {"from": str(int(start.timestamp() * 1000)),
            "to": str(int(end.timestamp() * 1000)), "timezone": timezone_name}


def panel_reason(panel):
    kind = panel.get("type")
    if kind not in {"stat", "timeseries", "piechart", "table", "geomap"}:
        return f"Unsupported renderer: {kind}; custom JavaScript is not executed"
    if panel.get("transformations"):
        return "Client-side transformation chain is unsupported (raw frames are not rendered table values)"
    if panel.get("timeFrom") or panel.get("timeShift") or panel.get("repeat"):
        return "Panel-specific time overrides/repeated panels are unsupported"
    if kind == "stat":
        reduce = panel.get("options", {}).get("reduceOptions", {})
        if reduce.get("values") is not False or reduce.get("fields") not in (None, "") or reduce.get("calcs") != ["sum"]:
            return "Only unfiltered sum stat reduction is supported"
    if kind == "piechart":
        reduce = panel.get("options", {}).get("reduceOptions", {})
        if reduce.get("values") is not True or reduce.get("fields") not in (None, ""):
            return "Only unfiltered all-values categorical pie data is supported"
    targets = panel.get("targets")
    if not isinstance(targets, list) or not any(isinstance(t, dict) and not t.get("hide") for t in targets):
        return "No active query target"
    return None


def frame_rows(frame):
    """Decode strict columnar Grafana DataFrames, including series context on every row."""
    if not isinstance(frame, dict) or not isinstance(frame.get("schema"), dict):
        raise GrafanaExtractionError("Malformed frame schema")
    schema = frame["schema"]
    fields = schema.get("fields")
    vectors = frame.get("data", {}).get("values") if isinstance(frame.get("data"), dict) else None
    if not isinstance(fields, list) or not fields or not isinstance(vectors, list) or len(fields) != len(vectors):
        raise GrafanaExtractionError("Frame field/vector count mismatch")
    names = [f.get("name") if isinstance(f, dict) else None for f in fields]
    if any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        raise GrafanaExtractionError("Missing/duplicate frame column names")
    if any(not isinstance(v, list) for v in vectors) or len({len(v) for v in vectors}) != 1:
        raise GrafanaExtractionError("Frame columns have unequal lengths")
    if "__series" in names:
        raise GrafanaExtractionError("Reserved series column collision")
    labels = []
    for f in fields:
        if f.get("type") not in {"time", "number", "string", "boolean"}:
            raise GrafanaExtractionError("Unsupported frame field type", "unsupported")
        if f.get("labels"):
            if not isinstance(f["labels"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in f["labels"].items()):
                raise GrafanaExtractionError("Invalid series labels")
            labels.append({"field": f["name"], "labels": f["labels"]})
    context = {"name": schema.get("name", ""), "labels": sorted(labels, key=canonical_json)}
    if not isinstance(context["name"], str):
        raise GrafanaExtractionError("Invalid series name")
    rows = []
    for index in range(len(vectors[0])):
        row = {}
        for f, vector in zip(fields, vectors):
            value, kind = vector[index], f.get("type")
            if kind not in {"time", "number", "string", "boolean"}:
                raise GrafanaExtractionError("Unsupported frame field type", "unsupported")
            if value is not None:
                if kind in {"time", "number"} and (type(value) not in (int, float) or not math.isfinite(value)):
                    raise GrafanaExtractionError("Invalid numeric/time field value")
                if kind == "string" and not isinstance(value, str) or kind == "boolean" and type(value) is not bool:
                    raise GrafanaExtractionError("Frame value does not match declared type")
                if kind == "time":
                    try:
                        value = datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")
                    except (ValueError, OverflowError, OSError) as exc:
                        raise GrafanaExtractionError("Time field is not valid epoch milliseconds") from exc
            row[f["name"]] = value
        if context["name"] or context["labels"]:
            row["__series"] = context
        rows.append(row)
    return rows, names + (["__series"] if context["name"] or context["labels"] else []), context


class GrafanaPublicAdapter:
    source_type = "grafana_public"

    def __init__(self, config, http=None, *, anchor=None):
        self.config = copy.deepcopy(config)
        self.base_url, self.public_id = public_identity(config["url"])
        self.api = self.base_url + "/api/public/dashboards/" + self.public_id
        self.http = http or GrafanaHTTPClient()
        self.anchor = anchor or datetime.now(timezone.utc)
        if self.anchor.tzinfo is None:
            raise GrafanaExtractionError("Range anchor must be timezone-aware")
        self.target_prefixes = (f"{self.source_type}:{self.public_id}:",)
        self.adapter_id = self.source_type + ":" + self.public_id
        self._targets = {}

    def discover(self):
        payload = self.http.get(self.api, allowed_origins={origin(self.api)}).json()
        if not isinstance(payload, dict) or not isinstance(payload.get("dashboard"), dict) or not isinstance(payload.get("meta"), dict):
            raise GrafanaExtractionError("Malformed public dashboard definition")
        dashboard, meta = payload["dashboard"], payload["meta"]
        if meta.get("publicDashboardEnabled") is not True:
            raise GrafanaExtractionError("Dashboard is not publicly enabled", "unsupported")
        if not all(isinstance(dashboard.get(k), str) and dashboard[k] for k in ("uid", "title")):
            raise GrafanaExtractionError("Public dashboard identity missing")
        if dashboard.get("templating", {}).get("list"):
            raise GrafanaExtractionError("Dashboard variables are unsupported", "unsupported")
        timezone_name = dashboard.get("timezone") or "UTC"
        if timezone_name == "browser":
            raise GrafanaExtractionError("Browser-dependent timezone requires explicit support", "unsupported")
        self._range = query_range(dashboard.get("time"), self.anchor, timezone_name)
        self._definition = dashboard
        self._updated = meta.get("updated")
        self._targets = {}
        self.non_data_panels = []
        seen = set()
        def visit(panels):
            if not isinstance(panels, list):
                raise GrafanaExtractionError("Invalid panel inventory")
            for panel in panels:
                if not isinstance(panel, dict) or type(panel.get("id")) is not int or panel["id"] < 0 or panel["id"] in seen:
                    raise GrafanaExtractionError("Invalid/duplicate panel ID")
                seen.add(panel["id"])
                if panel.get("type") == "row":
                    visit(panel.get("panels", []))
                    continue
                if panel.get("type") == "text":
                    self.non_data_panels.append(panel["id"])
                    continue
                name = (panel.get("title") or f"Panel {panel['id']}").strip()
                reason = panel_reason(panel)
                target = SourceTarget(self.source_type, dashboard["uid"], str(panel["id"]),
                                      self.config["url"], name,
                                      {"scope_id": self.public_id, "dashboard_name": dashboard["title"],
                                       "source_name": self.config.get("name", dashboard["title"]),
                                       "panel": panel, **({"skip_reason": reason} if reason else {})})
                self._targets[target.id] = target
        visit(dashboard.get("panels"))
        if not self._targets:
            raise GrafanaExtractionError("No data panels discovered", "unsupported")
        return sorted(self._targets.values(), key=lambda t: t.id)

    def extract(self, target):
        saved = self._targets.get(target.id)
        if saved != target:
            raise GrafanaExtractionError("Target was not discovered by this adapter")
        if target.options.get("skip_reason"):
            raise GrafanaExtractionError(target.options["skip_reason"], "unsupported")
        panel = target.options["panel"]
        refs = [t.get("refId") for t in panel["targets"] if not t.get("hide")]
        if any(not isinstance(r, str) or not r for r in refs) or len(set(refs)) != len(refs):
            raise GrafanaExtractionError("Missing/duplicate query references")
        payload = {"intervalMs": 60000, "maxDataPoints": 1000, "timeRange": self._range}
        response = self.http.query(self.api + f"/panels/{target.chart_id}/query", payload,
                                   allowed_origin=origin(self.api)).json()
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, dict) or set(results) != set(refs):
            raise GrafanaExtractionError("Missing/unexpected panel query results")
        parsed = []
        for ref in sorted(refs):
            result = results[ref]
            if not isinstance(result, dict):
                raise GrafanaExtractionError("Invalid panel query result")
            status = result.get("status", 200)
            if type(status) is not int:
                raise GrafanaExtractionError("Invalid panel query status")
            if status != 200:
                # Datasource failures can be wrapped in outer HTTP 200. Retain
                # shared retry/classification without logging source response bodies.
                error_response = requests.Response()
                error_response.status_code = status
                raise requests.HTTPError("Public panel datasource query failed", response=error_response)
            if result.get("error"):
                raise GrafanaExtractionError("Panel query failed; refusing partial target")
            frames = result.get("frames", [])
            if not isinstance(frames, list):
                raise GrafanaExtractionError("Invalid query frames")
            if not frames:
                if result.get("status") != 200:
                    raise GrafanaExtractionError("Empty query result lacks successful status")
                parsed.append((ref, {}, [], [], {}))
            for frame in frames:
                if not isinstance(frame, dict) or not isinstance(frame.get("schema"), dict) or frame["schema"].get("refId") != ref:
                    raise GrafanaExtractionError("Frame/query identity mismatch")
                rows, columns, context = frame_rows(frame)
                if panel["type"] == "stat":
                    number_fields = [f["name"] for f in frame["schema"]["fields"] if f["type"] == "number"]
                    if not number_fields:
                        raise GrafanaExtractionError("Sum stat has no numeric field")
                    sums = {}
                    for name in number_fields:
                        values = [r[name] for r in rows if r[name] is not None]
                        sums[name] = sum(values) if values else None
                    if rows:
                        if rows[0].get("__series"): sums["__series"] = rows[0]["__series"]
                        rows = [sums]
                        columns = list(sums)
                parsed.append((ref, frame["schema"], rows, columns, context))
        # Stable frame IDs independent of network frame arrival order. Ambiguous schemas fail;
        # values never determine record IDs and duplicate source rows are not deduplicated.
        parsed.sort(key=lambda p: canonical_json([p[0], p[1].get("name"), p[1].get("fields")]))
        identities = [canonical_json([p[0], p[1].get("name"), p[1].get("fields")]) for p in parsed]
        if len(set(identities)) != len(identities):
            raise GrafanaExtractionError("Ambiguous duplicate frame identities")
        records = []
        for index, (ref, schema, rows, columns, context) in enumerate(parsed):
            m = metadata_for(target, columns=columns, extraction_mode="grafana_public_panel_query",
                             is_complete=False, public_dashboard_id=self.public_id,
                             grafana_dashboard_uid=target.dashboard_id, grafana_panel_id=target.chart_id,
                             panel_type=panel["type"], query_ref_id=ref, frame_context=context,
                             field_schema=schema.get("fields", []), no_data=not rows,
                             statistical_period=self._definition["time"],
                             effective_filters={"saved_time_range": self._definition["time"],
                                                "timezone": self._range["timezone"]},
                             source_update_date=self._updated,
                             source_update_note="Dashboard definition update, NOT source-data freshness",
                             requested_time_range=self._range,
                             time_range_policy="Server saved range may override request; hidden time picker does not prove range capability",
                             pagination="Public panel query has no cursor/offset; saved aggregations/top-N limits apply",
                             data_semantics="Server-saved query DataFrame; sum applied for stat, no renderer JS or client transforms",
                             query_result_complete=True)
            records.append(NormalizedRecord(self.source_type, target.dashboard_id, target.chart_id,
                                            rows, m, target.id, index).normalized())
        return records
