"""Native CCTV adapter with bounded recursive next-link traversal."""
from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from contracts import SourceTarget, UnifiedRecord, metadata_for
from extractors.base import HTTPClient, origin

ENDPOINT = "https://idmc.jogjaprov.go.id/backend/api/v1/cctv"


class CCTVNativeAdapter:
    source_type = "cctv_native"

    def __init__(self, groups=("cctv-atcs",), http=None, max_pages=200):
        if not 1 <= max_pages <= 200:
            raise ValueError("max_pages must be between 1 and 200")
        self.groups, self.http, self.max_pages = groups, http or HTTPClient(), max_pages
        self.target_prefixes = tuple(f"{self.source_type}:{slug}:" for slug in groups)

    def discover(self):
        return [SourceTarget(
            self.source_type, slug, "cameras",
            ENDPOINT + "?" + urlencode({"filter[location]": slug}), slug,
        ) for slug in dict.fromkeys(self.groups)]

    def extract(self, target):
        seen_urls, cameras, expected_totals = set(), {}, set()
        expected_filter = parse_qs(urlparse(target.source_url).query).get("filter[location]")

        def visit(url, depth=0):
            parsed = urlparse(url)
            if origin(url) != origin(ENDPOINT) or parsed.path != urlparse(ENDPOINT).path:
                raise ValueError("CCTV next link escaped the configured endpoint")
            if parse_qs(parsed.query).get("filter[location]") != expected_filter:
                raise ValueError("CCTV next link dropped or changed the location filter")
            if url in seen_urls or depth >= self.max_pages:
                raise ValueError("CCTV pagination cycle or page limit exceeded")
            seen_urls.add(url)
            payload = self.http.get(url, allowed_origins={origin(ENDPOINT)}).json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Expected CCTV {data, links, meta} response")
            if payload.get("meta", {}).get("total") is not None:
                expected_totals.add(int(payload["meta"]["total"]))
            for row in payload["data"]:
                if not isinstance(row, dict) or row.get("id") is None:
                    raise ValueError("Camera record has no ID")
                if row.get("location") != target.dashboard_id:
                    raise ValueError("CCTV response contains a different location")
                key = str(row["id"])
                if key in cameras and cameras[key] != row:
                    raise ValueError("Camera changed across pagination; retry a consistent snapshot")
                cameras[key] = row
            nxt = payload.get("links", {}).get("next")
            if nxt:
                visit(urljoin(url, nxt), depth + 1)

        visit(target.source_url)
        if len(expected_totals) > 1 or (expected_totals and len(cameras) != next(iter(expected_totals))):
            raise ValueError("CCTV page totals changed or deduplicated count differs from meta.total")
        rows = [cameras[k] for k in sorted(cameras)]
        return [UnifiedRecord(
            self.source_type, target.dashboard_id, target.chart_id, rows,
            metadata_for(
                target, unit="camera", statistical_period="snapshot",
                columns=sorted({k for row in rows for k in row}),
                is_complete=True if expected_totals else "unknown", pages_fetched=len(seen_urls),
                extraction_mode="api", expected_total=next(iter(expected_totals), None),
            ), target.id,
        )]
