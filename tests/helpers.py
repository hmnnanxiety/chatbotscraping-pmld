import copy
from unittest.mock import Mock
import requests
from contracts import SourceTarget, NormalizedRecord, metadata_for

FIXED_TIME = "2026-09-29T12:00:00+00:00"
LATER = "2026-09-30T12:00:00+00:00"

def record(chart="1", rows=None, dashboard="demo"):
    target = SourceTarget("fixture", dashboard, chart, "https://example.org/data", "Chart " + chart,
                          {"dashboard_name": "Example dashboard", "source_name": "Example source"})
    meta = metadata_for(target, last_checked_at=FIXED_TIME, last_successful_fetch_at=FIXED_TIME,
                        statistical_period="2026", unit="people", columns=["count"])
    return NormalizedRecord("fixture", dashboard, chart, rows if rows is not None else [{"count": 42}], meta, target.id).normalized()

class FakeAdapter:
    source_type = "fixture"
    def __init__(self, records, failures=None, discovery_error=None):
        self.records = records
        self.failures = failures or {}
        self.discovery_error = discovery_error
        self.target_prefixes = tuple(sorted({r.target_id.rsplit(":", 1)[0] + ":" for r in records}))
        self.adapter_id = "fixture:" + (records[0].dashboard_id if records else "empty")
    def discover(self):
        if self.discovery_error:
            raise self.discovery_error
        return [SourceTarget(r.source_type, r.dashboard_id, r.chart_id, r.metadata["source_url"],
                             r.metadata["chart_name"], {"dashboard_name": r.metadata["dashboard_name"]})
                for r in self.records]
    def extract(self, target):
        if target.chart_id in self.failures:
            raise self.failures[target.chart_id]
        return [copy.deepcopy(r) for r in self.records if r.target_id == target.id]

def http_error(status):
    return requests.HTTPError(response=Mock(status_code=status))
