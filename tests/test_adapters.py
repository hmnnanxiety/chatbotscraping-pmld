import copy
import html
import json
import unittest
from unittest.mock import Mock

import requests
from tenacity import wait_none

from contracts import SourceTarget, UnifiedRecord, metadata_for
from extractors.base import HTTPClient, origin
from extractors.cctv import CCTVNativeAdapter
from extractors.superset import BootstrapParser, SupersetAdapter, effective_query
from extractors.tableau_looker import TableauLookerAdapter
from extractor import execute_adapter


def response(payload=None, text="", status=200, headers=None):
    result = Mock()
    result.status_code, result.text = status, text
    result.content = text.encode("utf-8")
    result.headers = headers or {}
    result.json.return_value = payload
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class SupersetTests(unittest.TestCase):
    def setUp(self):
        self.body = {"queries": [{"filters": [{"col": "bulan", "op": "IN", "val": ["Desember"]}]}], "form_data": {}}
        self.dashboard = {"id": 14, "json_metadata": {
            "native_filter_configuration": [{
                "chartsInScope": [608], "scope": {"excluded": []},
                "defaultDataMask": {"extraFormData": {"filters": [{"col": "tahun", "op": "IN", "val": [2026]}]}},
            }]}}

    def test_default_year_preserves_chart_month_and_input(self):
        old = copy.deepcopy(self.body)
        result = effective_query(self.body, self.dashboard, 608)
        self.assertEqual([f["col"] for f in result["queries"][0]["filters"]], ["bulan", "tahun"])
        self.assertEqual(result["form_data"]["dashboard_id"], 14)
        self.assertFalse(result["force"])
        self.assertEqual(self.body, old)

    def test_excluded_and_out_of_scope_chart(self):
        nf = self.dashboard["json_metadata"]["native_filter_configuration"][0]
        nf["scope"]["excluded"] = [608]
        self.assertEqual(len(effective_query(self.body, self.dashboard, 608)["queries"][0]["filters"]), 1)
        nf["scope"]["excluded"] = []
        self.assertEqual(len(effective_query(self.body, self.dashboard, 618)["queries"][0]["filters"]), 1)

    def test_filter_applies_to_every_query_and_deduplicates(self):
        self.body["queries"].append(copy.deepcopy(self.body["queries"][0]))
        result = effective_query(self.body, self.dashboard, 608)
        result2 = effective_query(result, self.dashboard, 608)
        self.assertTrue(all(len(q["filters"]) == 2 for q in result2["queries"]))

    def test_time_range_and_grain_mapping(self):
        nf = self.dashboard["json_metadata"]["native_filter_configuration"][0]
        nf["defaultDataMask"]["extraFormData"] = {"time_range": "2026-01-01 : 2027-01-01", "time_grain_sqla": "P1M"}
        q = effective_query(self.body, self.dashboard, 608)["queries"][0]
        self.assertEqual(q["extras"]["time_grain_sqla"], "P1M")
        self.assertIn("2026", q["time_range"])

    def test_unknown_filter_fails_closed(self):
        nf = self.dashboard["json_metadata"]["native_filter_configuration"][0]
        nf["defaultDataMask"]["extraFormData"] = {"unhandled_filter": True}
        with self.assertRaises(ValueError):
            effective_query(self.body, self.dashboard, 608)

    def test_layout_scope(self):
        nf = self.dashboard["json_metadata"]["native_filter_configuration"][0]
        del nf["chartsInScope"]
        nf["scope"]["rootPath"] = ["ROOT_ID", "TAB-A"]
        self.dashboard["position_json"] = {"TAB-A": {"children": ["CHART-A"]}, "CHART-A": {"meta": {"chartId": 608}}}
        self.assertEqual(len(effective_query(self.body, self.dashboard, 608)["queries"][0]["filters"]), 2)
        self.assertEqual(len(effective_query(self.body, self.dashboard, 609)["queries"][0]["filters"]), 1)

    def test_bootstrap_entity_decoding_not_generic_id(self):
        parser = BootstrapParser()
        parser.feed('<div id="app" data-bootstrap="' + html.escape(json.dumps({"common": {"id": 999}, "embedded": {"dashboard_id": 83}}), quote=True) + '"></div>')
        self.assertEqual(parser.dashboard_id, 83)

    def test_multiple_result_sets_have_distinct_ids(self):
        client = Mock()
        adapter = SupersetAdapter({"uuid": "uuid", "name": "Test", "numeric_id": 14}, client)
        adapter.dashboard = self.dashboard
        self.body["queries"].append(copy.deepcopy(self.body["queries"][0]))
        client._authed_get.return_value = response({"result": {"query_context": json.dumps(self.body)}})
        client._authed_post.return_value = response({"result": [
            {"data": [[123]], "colnames": ["count"], "status": "success"},
            {"data": [{"count": 456}], "colnames": ["count"], "status": "success"},
        ]})
        target = SourceTarget("superset", "14", "608", "https://dwh.jogjaprov.go.id/", "Test", {"scope_id": "uuid"})
        result = adapter.extract(target)
        self.assertEqual(len({r.id for r in result}), 2)
        self.assertEqual(result[0].raw_rows, [{"count": 123}])
        self.assertEqual(result[0].metadata["statistical_period"]["filters"][0]["val"], [2026])


class CCTVTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.adapter = CCTVNativeAdapter(http=self.http)
        self.target = self.adapter.discover()[0]
        self.next = self.target.source_url + "&page=2"

    def page(self, start, end, nxt=None, total=148):
        return response({"data": [{"id": i, "location": "cctv-atcs", "connection": 1} for i in range(start, end)],
                         "links": {"next": nxt}, "meta": {"total": total}})

    def test_collects_148_across_two_pages(self):
        self.http.get.side_effect = [self.page(0, 100, self.next), self.page(100, 148)]
        record = self.adapter.extract(self.target)[0]
        self.assertEqual(len(record.raw_rows), 148)
        self.assertEqual(record.metadata["pages_fetched"], 2)
        self.assertTrue(record.metadata["is_complete"])

    def test_overlap_deduplicates_ids(self):
        self.http.get.side_effect = [self.page(0, 100, self.next), self.page(99, 148)]
        self.assertEqual(len(self.adapter.extract(self.target)[0].raw_rows), 148)

    def test_cycle_rejected(self):
        self.http.get.return_value = self.page(0, 100, self.target.source_url)
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.adapter.extract(self.target)

    def test_cross_host_and_filter_drop_rejected(self):
        for nxt in ["https://example.com/cctv?page=2", "https://idmc.jogjaprov.go.id/backend/api/v1/cctv?page=2"]:
            self.http.get.return_value = self.page(0, 100, nxt)
            with self.assertRaises(ValueError):
                self.adapter.extract(self.target)

    def test_incomplete_total_does_not_publish(self):
        self.http.get.return_value = self.page(0, 100)
        with self.assertRaisesRegex(ValueError, "count differs"):
            self.adapter.extract(self.target)

    def test_failed_second_page_does_not_return_partial_rows(self):
        self.http.get.side_effect = [self.page(0, 100, self.next), requests.Timeout()]
        with self.assertRaises(requests.Timeout):
            self.adapter.extract(self.target)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.source = {"id": "education", "name": "Education", "url": "https://public.tableau.com/views/a/b"}

    def test_metadata_only_preserves_export_links(self):
        self.http.get.return_value = response(text='<title>Education</title><meta name="description" content="Students"><a href="/data.pdf">PDF</a>')
        adapter = TableauLookerAdapter([self.source], self.http)
        record = adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(record.raw_rows, [])
        self.assertEqual(record.metadata["extraction_mode"], "metadata_only")
        self.assertEqual(record.metadata["available_exports"], ["https://public.tableau.com/data.pdf"])

    def test_explicit_csv_export(self):
        self.source["export"] = {"url": "https://public.tableau.com/data.csv", "format": "csv"}
        self.http.get.side_effect = [response(text="<title>Table</title>"), response(text="year,count\n2026,42\n", headers={"Content-Type": "text/csv"})]
        adapter = TableauLookerAdapter([self.source], self.http)
        record = adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(record.raw_rows, [{"year": "2026", "count": "42"}])
        self.assertEqual(record.metadata["extraction_mode"], "export")

    def test_html_export_falls_back_with_warning(self):
        self.source["export"] = {"url": "https://public.tableau.com/data.csv", "format": "csv"}
        self.http.get.side_effect = [response(text="<title>Table</title>"), response(text="<html>Unavailable</html>", headers={"Content-Type": "text/html"})]
        adapter = TableauLookerAdapter([self.source], self.http)
        record = adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(record.metadata["extraction_mode"], "metadata_only")
        self.assertIn("extraction_warning", record.metadata)


class RetryTests(unittest.TestCase):
    def test_timeout_retried_then_succeeds(self):
        adapter = Mock()
        adapter.extract.side_effect = [requests.Timeout(), ["done"]]
        execute = execute_adapter.retry_with(wait=wait_none())
        self.assertEqual(execute(adapter, None), ["done"])
        self.assertEqual(adapter.extract.call_count, 2)

    def test_404_not_retried(self):
        adapter = Mock()
        adapter.extract.side_effect = requests.HTTPError(response=response(status=404))
        with self.assertRaises(requests.HTTPError):
            execute_adapter.retry_with(wait=wait_none())(adapter, None)
        self.assertEqual(adapter.extract.call_count, 1)

    def test_cross_origin_redirect_rejected(self):
        session = Mock()
        session.get.return_value = response(status=302, headers={"Location": "https://example.com/"})
        with self.assertRaisesRegex(ValueError, "Cross-origin"):
            HTTPClient(session=session, min_interval=0).get("https://idmc.jogjaprov.go.id/backend/api/v1/cctv")
        self.assertEqual(session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
