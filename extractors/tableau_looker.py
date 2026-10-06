"""Public-page metadata with explicitly configured, permitted CSV/JSON exports."""
from __future__ import annotations

import csv
import io
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from contracts import SourceTarget, UnifiedRecord, metadata_for
from extractors.base import HTTPClient, origin, retry_source


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title = ""
        self.description = ""
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta" and attrs.get("name", "").lower() == "description":
            self.description = attrs.get("content", "")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


class TableauLookerAdapter:
    source_type = "tableau_looker"

    def __init__(self, sources, http=None):
        self.sources, self.http = sources, http or HTTPClient()
        self.target_prefixes = tuple(f"{self.source_type}:{s['id']}:" for s in sources)
        self.adapter_id = ",".join(prefix.rstrip(":") for prefix in self.target_prefixes)

    def discover(self):
        targets = []
        for source in self.sources:
            host = urlparse(source["url"]).hostname or ""
            if host not in {"public.tableau.com", "lookerstudio.google.com", "datastudio.google.com"}:
                raise ValueError(f"Unsupported metadata platform: {host}")
            targets.append(SourceTarget(
                self.source_type, source["id"], "view", source["url"], source["name"], source,
            ))
        return targets

    def extract(self, target):
        allowed = {origin(target.source_url)}
        if urlparse(target.source_url).hostname in {"lookerstudio.google.com", "datastudio.google.com"}:
            allowed |= {origin("https://lookerstudio.google.com"), origin("https://datastudio.google.com")}
        page = self.http.get(target.source_url, allowed_origins=allowed)
        parsed = PageMetadata()
        parsed.feed(page.text)
        links = [urljoin(target.source_url, link) for link in parsed.links
                 if urlparse(link).path.lower().endswith((".csv", ".json", ".pdf", ".xlsx"))]
        meta = metadata_for(
            target, title=parsed.title.strip() or target.name, description=parsed.description,
            platform="tableau" if "tableau" in urlparse(target.source_url).hostname else "looker",
            extraction_mode="metadata_only", available_exports=sorted(set(links)),
            is_complete=False,
        )
        export = target.options.get("export")
        rows = []
        if export:
            # No guessed download routes, credentials, or permission workarounds.
            url = export["url"]
            if origin(url) not in allowed:
                raise ValueError("Configured export must belong to the report's origin")
            try:
                response = retry_source(self.http.get)(url, allowed_origins=allowed)
                content_type = response.headers.get("Content-Type", "").lower()
                if export["format"] == "csv":
                    if "html" in content_type or response.text.lstrip().startswith("<"):
                        raise ValueError("Export returned HTML instead of CSV")
                    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
                    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                        raise ValueError("CSV requires nonempty, unique column names")
                    parsed_rows = list(reader)
                    if any(None in row or any(value is None for value in row.values()) for row in parsed_rows):
                        raise ValueError("CSV rows have inconsistent column counts")
                    rows = parsed_rows
                elif export["format"] == "json":
                    payload = response.json()
                    rows = payload.get("data") if isinstance(payload, dict) else payload
                    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                        raise ValueError("Export JSON must contain a list of row objects")
                else:
                    raise ValueError("Only structured CSV and JSON exports are supported")
                meta.update(extraction_mode="export", export_url=url, is_complete="unknown")
            except (requests.RequestException, ValueError) as exc:
                rows = []
                meta["extraction_warning"] = f"Export unavailable ({type(exc).__name__}); metadata only"
        return [UnifiedRecord(
            self.source_type, target.dashboard_id, target.chart_id, rows, meta, target.id,
        )]
