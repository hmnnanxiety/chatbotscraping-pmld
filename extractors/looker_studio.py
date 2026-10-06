"""Reusable, strict Looker Studio rendered-table ingestion (no private RPCs)."""
from __future__ import annotations
import re
from urllib.parse import urlparse

from contracts import SourceTarget, UnifiedRecord, metadata_for
from etl_common import canonical_json


class LookerExtractionError(ValueError):
    def __init__(self, message, category="validation"):
        super().__init__(message)
        self.ingestion_category = category


def report_identity(url):
    p = urlparse(url)
    match = re.fullmatch(r"/(?:embed/)?reporting/([A-Za-z0-9_-]+)(?:/page/([A-Za-z0-9_-]+))?/?", p.path)
    if p.scheme != "https" or p.hostname not in {"lookerstudio.google.com", "datastudio.google.com"} or p.port not in {None, 443} or \
            p.username or p.password or not match or p.query or p.fragment:
        raise LookerExtractionError("Expected a public Looker report/page URL without extra filter parameters")
    return match.group(1), match.group(2)


def collect_table(pages, *, max_pages=200, max_rows=100000):
    """Coverage by position, NOT row-value deduplication: equal data rows are legal."""
    columns, rows, total, next_row, count = None, [], None, 1, 0
    for page in pages:
        count += 1
        if count > max_pages:
            raise LookerExtractionError("Looker pagination page limit exceeded")
        header = page.get("columns")
        if not isinstance(header, list) or not header or any(not isinstance(c, str) or not c.strip() for c in header) or \
                len(set(header)) != len(header):
            raise LookerExtractionError("Looker table requires nonempty unique column names")
        if columns is None:
            columns = header
        if columns != header:
            raise LookerExtractionError("Looker columns changed during pagination")
        first, last, expected = (page.get(k) for k in ("row_start", "row_end", "total_rows"))
        if any(type(x) is not int for x in (first, last, expected)) or not 0 <= expected <= max_rows:
            raise LookerExtractionError("Invalid Looker row range/total")
        if total is None:
            total = expected
        if total != expected:
            raise LookerExtractionError("Looker total changed during pagination")
        values = page.get("rows")
        if not isinstance(values, list) or any(not isinstance(r, list) or len(r) != len(columns) or
                                              any(not isinstance(v, str) for v in r) for r in values):
            raise LookerExtractionError("Looker row width/value shape invalid")
        if expected == 0:
            if count != 1 or values or (first, last) != (0, 0):
                raise LookerExtractionError("Invalid empty Looker table")
        elif first != next_row or not first <= last <= total or len(values) != last - first + 1:
            raise LookerExtractionError("Looker rows lost, overlapping, or incomplete")
        rows.extend(dict(zip(columns, r)) for r in values)
        next_row = last + 1
    if not count or len(rows) != total:
        raise LookerExtractionError("Looker pagination ended before the verified total")
    return rows, columns, count


class LookerStudioAdapter:
    source_type = "looker_studio"

    def __init__(self, source, browser_factory=None, max_pages=200, max_rows=100000):
        self.config = source
        self.report_id, self.page_id = report_identity(source["url"])
        if not source.get("id") or not source.get("name"):
            raise LookerExtractionError("Looker source id/name required")
        if not 1 <= max_pages <= 200 or not 1 <= max_rows <= 100000:
            raise ValueError("Looker pagination limits outside supported bounds")
        self.max_pages, self.max_rows = max_pages, max_rows
        self.adapter_id = f"{self.source_type}:{self.report_id}"
        self.target_prefixes = (self.adapter_id + ":",)
        if browser_factory is None:
            from extractors.looker_browser import LookerBrowser
            browser_factory = LookerBrowser
        self.browser_factory = browser_factory

    def discover(self):
        with self.browser_factory(self.config) as browser:
            catalog = browser.discover()
        if not isinstance(catalog, list) or not catalog:
            raise LookerExtractionError("No Looker charts discovered", "unsupported")
        ids, targets = set(), []
        for chart in sorted(catalog, key=lambda c: c.get("component_id", "")):
            cid = chart.get("component_id")
            if not isinstance(cid, str) or not re.fullmatch(r"cd-[A-Za-z0-9_-]+", cid) or cid in ids:
                raise LookerExtractionError("Missing/duplicate stable Looker component ID")
            ids.add(cid)
            page = chart.get("page_id") or self.page_id
            if page is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", page):
                raise LookerExtractionError("Invalid Looker page ID")
            if self.page_id and page != self.page_id:
                raise LookerExtractionError("Looker discovery escaped configured portal page")
            options = {"dashboard_name": self.config["name"], "report_config_id": self.config["id"],
                       "report_id": self.report_id, "report_page_id": page,
                       "component_id": cid, "chart_kind": chart.get("kind"),
                       "discovery_catalog": catalog, "health_scope": self.adapter_id}
            if chart.get("kind") not in {"table", "accessible_table"}:
                options["skip_reason"] = "unsupported: Looker chart has no supported structured table representation"
            targets.append(SourceTarget(self.source_type, self.report_id, f"{page or 'landing'}/{cid}",
                                        self.config["url"], chart.get("title") or f"Table {cid}", options))
        if all(t.options.get("skip_reason") for t in targets):
            raise LookerExtractionError("Report has no supported structured table charts", "unsupported")
        return targets

    def extract(self, target):
        if target.dashboard_id != self.report_id or target.source_url != self.config["url"]:
            raise LookerExtractionError("Looker target escaped configured report")
        if target.options.get("chart_kind") not in {"table", "accessible_table"}:
            raise LookerExtractionError("Unsupported Looker chart", "unsupported")
        with self.browser_factory(self.config) as browser:
            rows, columns, count = collect_table(browser.pages(target.options["component_id"]),
                                                max_pages=self.max_pages, max_rows=self.max_rows)
        # Displayed values remain strings: no guessing locale, units, nulls, or types.
        meta = metadata_for(target, platform="looker_studio", extraction_mode="browser_table",
                            columns=columns, is_complete=True, pages_fetched=count,
                            report_id=self.report_id, report_page_id=target.options["report_page_id"],
                            report_config_id=self.config["id"], component_id=target.options["component_id"],
                            chart_kind=target.options["chart_kind"], value_encoding="display_strings",
                            extraction_scope="configured portal landing page; rendered chart result only",
                            filter_binding="portal default; no interactive filter changes",
                            discovery_catalog=target.options["discovery_catalog"])
        record = UnifiedRecord(self.source_type, target.dashboard_id, target.chart_id, rows, meta, target.id).normalized()
        canonical_json(record.to_dict())
        return [record]
