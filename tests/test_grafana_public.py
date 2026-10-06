"""Small synthetic public DataFrames; no production snapshots or browser required."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests
from etl_common import canonical_json
from extractor import extract_all, ExtractionAborted, classify_error
from extractors.base import origin, transient_error
from extractors.grafana_public import (GrafanaPublicAdapter, GrafanaHTTPClient,
    GrafanaExtractionError, public_identity, frame_rows, query_range)
from extractors.registry import source_inventory, configured_adapters
from portal_scope import PortalScope, source_key
from transformers import build_documents, validate_chunks
from tests.helpers import FIXED_TIME, LATER

SOURCE = {"id": "synthetic", "name": "Internal configured source",
          "url": "https://public.example.test/public-dashboards/" + "a" * 32}
ANCHOR = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def panel(identity=1, kind="table"):
    options = {}
    if kind == "stat": options = {"reduceOptions": {"calcs": ["sum"], "fields": "", "values": False}}
    if kind == "piechart": options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}}
    return {"id": identity, "type": kind, "title": "Synthetic panel " + str(identity),
            "targets": [{"refId": "A"}], "options": options}


def definition(panels=None):
    return {"meta": {"publicDashboardEnabled": True, "updated": "2026-01-01T00:00:00Z"},
            "dashboard": {"uid": "internal-uid", "title": "Internal dashboard", "timezone": "",
                          "time": {"from": "now-7d", "to": "now"}, "templating": {"list": []},
                          "panels": panels if panels is not None else [panel()]}}


def frame(name="Series", values=None, fields=None):
    return {"schema": {"name": name, "refId": "A", "fields": fields or [
        {"name": "category", "type": "string"}, {"name": "Count", "type": "number"}]},
        "data": {"values": values if values is not None else [["one", "two"], [3, 5]]}}


def query(frames=None):
    return {"results": {"A": {"status": 200, "frames": frames if frames is not None else [frame()]}}}


def adapter(panels=None, response=None):
    http = Mock()
    http.get.return_value.json.return_value = definition(panels)
    http.query.return_value.json.return_value = response if response is not None else query()
    return GrafanaPublicAdapter(SOURCE, http, anchor=ANCHOR)


def scope(active=1):
    return PortalScope.from_payloads(source_inventory({"superset": False, "grafana_sources": [SOURCE]}),
        [{"id": 1, "name": "Social Media Analytic", "status": active, "children": [
            {"id": 2, "name": "Yogyakarta", "status": 1, "source": SOURCE["url"]}]}],
        {"data": [{"dashboard_url": None}]})


class GrafanaTests(unittest.TestCase):
    def extract(self, a=None):
        a = a or adapter()
        targets = a.discover()
        with patch("contracts.utcnow", return_value=FIXED_TIME):
            return a.extract(targets[0])

    def test_public_identity(self):
        self.assertEqual(public_identity(SOURCE["url"]), ("https://public.example.test", "a" * 32))

    def test_invalid_identity_rejected(self):
        for url in (SOURCE["url"].replace("https:", "http:"), SOURCE["url"] + "?from=now",
                    SOURCE["url"] + "#panel", SOURCE["url"].replace("https://", "https://user@"),
                    "https://host.test/d/private", "https://host.test/public-dashboards/bad"):
            with self.subTest(url=url), self.assertRaises(ValueError): public_identity(url)

    def test_portal_exact_mapping_preserves_internal_and_portal_names(self):
        a = adapter(); r = self.extract(a)[0]
        records, excluded = scope().apply({r.id: r})
        self.assertEqual(excluded, [])
        m = records[r.id].metadata
        self.assertEqual(m["portal_page_name"], "Yogyakarta")
        self.assertEqual(m["source_dashboard_name"], "Internal dashboard")
        self.assertEqual(m["public_dashboard_id"], "a" * 32)
        self.assertEqual(r.dashboard_id, "internal-uid")
        self.assertNotEqual(source_key(SOURCE["url"]), source_key(SOURCE["url"].replace("example.test", "other.test")))
        self.assertIsNone(source_key(SOURCE["url"] + "?from=now"))

    def test_hidden_scope_excludes_records(self):
        r = self.extract()[0]
        self.assertEqual(scope(0).apply({r.id: r}), ({}, [r.id]))

    def test_registry_configuration_and_wiring(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "ingestion_sources.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(len(config["grafana_sources"]), 2)
        self.assertEqual(len({s["url"] for s in config["grafana_sources"]}), 2)
        with patch("portal_scope.fetch_scope", return_value=scope()), patch("extractors.registry.GrafanaPublicAdapter") as factory:
            configured_adapters(root / "ingestion_sources.json")
        factory.assert_called_once_with(SOURCE)

    def test_relative_range_fixed_anchor(self):
        r = query_range({"from": "now-7d", "to": "now"}, ANCHOR, "UTC")
        self.assertEqual(int(r["to"]) - int(r["from"]), 7 * 86400000)
        self.assertEqual(r, query_range({"from": "now-7d", "to": "now"}, ANCHOR, "UTC"))

    def test_unsupported_date_math_and_reverse_range_fail(self):
        for saved in ({"from": "now-7d/d", "to": "now"}, {"from": "now", "to": "now-1h"}):
            with self.assertRaises(ValueError): query_range(saved, ANCHOR, "UTC")

    def test_nested_panels_and_non_data(self):
        a = adapter([{"id": 90, "type": "row", "panels": [panel()]}, {"id": 91, "type": "text"}])
        self.assertEqual(len(a.discover()), 1); self.assertEqual(a.non_data_panels, [91])

    def test_duplicate_panel_ids_fail(self):
        with self.assertRaises(ValueError): adapter([panel(), panel()]).discover()

    def test_disabled_dashboard_missing_identity_and_variables_fail(self):
        for change in ("disabled", "uid", "variables"):
            a = adapter(); d = definition()
            if change == "disabled": d["meta"]["publicDashboardEnabled"] = False
            elif change == "uid": d["dashboard"].pop("uid")
            else: d["dashboard"]["templating"]["list"] = [{"name": "var"}]
            a.http.get.return_value.json.return_value = d
            with self.subTest(change=change), self.assertRaises(ValueError): a.discover()

    def test_custom_plugin_and_transform_explicitly_unsupported(self):
        transformed = panel(2); transformed["transformations"] = [{"id": "groupBy"}]
        a = adapter([panel(1, "volkovlabs-echarts-panel"), transformed])
        for t in a.discover():
            self.assertTrue(t.options["skip_reason"])
            with self.assertRaises(GrafanaExtractionError) as ctx: a.extract(t)
            self.assertEqual(classify_error(ctx.exception), "unsupported")
        a.http.query.assert_not_called()

    def test_time_override_and_unsupported_stat_skipped(self):
        p = panel(kind="stat"); p["options"]["reduceOptions"]["calcs"] = ["lastNotNull"]
        shifted = panel(2); shifted["timeShift"] = "1d"
        self.assertTrue(all(t.options.get("skip_reason") for t in adapter([p, shifted]).discover()))

    def test_query_only_saved_server_targets_and_range(self):
        a = adapter(); self.extract(a)
        args, kwargs = a.http.query.call_args
        self.assertTrue(args[0].endswith("/panels/1/query"))
        self.assertEqual(set(args[1]), {"timeRange", "intervalMs", "maxDataPoints"})
        self.assertEqual(kwargs["allowed_origin"], origin(SOURCE["url"]))

    def test_successful_rows_not_metadata_only(self):
        r = self.extract()[0]
        self.assertEqual(len(r.raw_rows), 2)
        self.assertFalse(r.metadata["is_complete"])
        self.assertTrue(r.metadata["query_result_complete"])
        self.assertIn("NOT source-data", r.metadata["source_update_note"])
        self.assertEqual(r.metadata["extraction_mode"], "grafana_public_panel_query")

    def test_time_field_epoch_milliseconds_and_labels(self):
        f = frame(values=[[0, 1000], [2, 3]], fields=[{"name": "Time", "type": "time"},
             {"name": "Value", "type": "number", "labels": {"sentiment": "positive"}}])
        rows, _, _ = frame_rows(f)
        self.assertEqual(rows[0]["Time"], "1970-01-01T00:00:00.000+00:00")
        self.assertEqual(rows[0]["__series"]["labels"][0]["labels"], {"sentiment": "positive"})

    def test_sum_stat_matches_visible_reduction(self):
        a = adapter([panel(kind="stat")], query([frame(values=[["a", "b"], [2, 3]])]))
        self.assertEqual(self.extract(a)[0].raw_rows[0]["Count"], 5)

    def test_stat_null_not_fabricated_zero(self):
        a = adapter([panel(kind="stat")], query([frame(values=[["a", "b"], [None, None]])]))
        self.assertIsNone(self.extract(a)[0].raw_rows[0]["Count"])

    def test_successful_no_data_is_explicit_not_metadata_fallback(self):
        a = adapter(response={"results": {"A": {"status": 200}}})
        r = self.extract(a)[0]
        self.assertEqual(r.raw_rows, []); self.assertTrue(r.metadata["no_data"])
        docs = build_documents({r.id: r}); self.assertIn("zero rows", docs[0]["page_content"])

    def test_empty_missing_results_not_success(self):
        for p in ({}, {"results": {}}, {"results": {"A": {}}}):
            with self.subTest(payload=p), self.assertRaises(ValueError): self.extract(adapter(response=p))

    def test_wrapped_http_failure_preserves_retry_and_classification(self):
        for status, retried in ((429, True), (503, True), (404, False), (403, False)):
            with self.subTest(status=status), self.assertRaises(requests.HTTPError) as ctx:
                self.extract(adapter(response={"results": {"A": {"status": status}}}))
            self.assertEqual(transient_error(ctx.exception), retried)
            self.assertEqual(classify_error(ctx.exception), "auth" if status == 403 else f"http_{status}")

    def test_boolean_null_and_invalid_time(self):
        f = frame(values=[[True, None]], fields=[{"name": "flag", "type": "boolean"}])
        self.assertEqual([r["flag"] for r in frame_rows(f)[0]], [True, None])
        f["data"]["values"] = [[1]]
        with self.assertRaises(ValueError): frame_rows(f)
        f = frame(values=[[1e99]], fields=[{"name": "Time", "type": "time"}])
        with self.assertRaises(ValueError): frame_rows(f)

    def test_frame_context_and_changed_value_affect_content_hash(self):
        r = self.extract()[0]
        f = frame(); f["data"]["values"][1][0] = 99
        changed = self.extract(adapter(response=query([f])))[0]
        self.assertNotEqual(r.content_hash, changed.content_hash)
        renamed = self.extract(adapter(response=query([frame("New series")])))[0]
        self.assertNotEqual(r.content_hash, renamed.content_hash)

    def test_post_custom_ca_and_pacing(self):
        session = Mock(); session.post.return_value.status_code = 200
        with patch("extractors.base.tls_verify", return_value="synthetic-ca.pem"):
            h = GrafanaHTTPClient(session=session, timeout=30, min_interval=2)
        h._last = 100
        with patch("extractors.grafana_public.time.monotonic", return_value=101), patch("extractors.grafana_public.time.sleep") as sleep:
            h.query("https://public.example.test/q", {}, allowed_origin=origin(SOURCE["url"]))
        sleep.assert_called_once_with(1)
        self.assertEqual(session.post.call_args.kwargs["verify"], "synthetic-ca.pem")

    def test_partial_query_failure_rejects_whole_target(self):
        p = panel(); p["targets"].append({"refId": "B"})
        q = query(); q["results"]["B"] = {"status": 200, "error": "synthetic failure"}
        with self.assertRaises(ValueError): self.extract(adapter([p], q))

    def test_malformed_frame_shapes_fail(self):
        bad = []
        f = frame(); f["data"]["values"][1].pop(); bad.append(f)
        f = frame(); f["schema"]["fields"][1]["name"] = "category"; bad.append(f)
        f = frame(); f["schema"]["refId"] = "wrong"; bad.append(f)
        f = frame(); f["data"]["values"][1][0] = float("nan"); bad.append(f)
        f = frame(); f["data"]["values"][1][0] = "3"; bad.append(f)
        f = frame(); f["schema"]["fields"][0]["type"] = "other"; bad.append(f)
        for f in bad:
            with self.subTest(frame=f), self.assertRaises(ValueError): self.extract(adapter(response=query([f])))

    def test_ambiguous_duplicate_frames_fail(self):
        with self.assertRaises(ValueError): self.extract(adapter(response=query([frame(), frame()])))

    def test_frame_order_does_not_change_record_ids_or_chunks(self):
        frames = [frame("Second"), frame("First")]
        first = self.extract(adapter(response=query(frames)))
        second = self.extract(adapter(response=query(list(reversed(frames)))))
        self.assertEqual(canonical_json([r.to_dict() for r in first]), canonical_json([r.to_dict() for r in second]))
        self.assertEqual(build_documents({r.id: r for r in first}), build_documents({r.id: r for r in second}))

    def test_equal_source_rows_preserved_and_chunk_coverage(self):
        f = frame(values=[["equal"] * 40, [2] * 40])
        records = self.extract(adapter(response=query([f])))
        r = records[0]; docs = build_documents({r.id: r}, max_chars=600)
        self.assertGreater(len(docs), 1); self.assertEqual(len(r.raw_rows), 40)
        self.assertTrue(validate_chunks({r.id: r}, docs, max_chars=600))
        self.assertEqual(sum(d["metadata"]["row_end"]-d["metadata"]["row_start"]+1 for d in docs), 40)

    def test_timestamp_only_changes_do_not_change_content_hash(self):
        r = self.extract()[0]; other = copy.deepcopy(r)
        other.metadata.update(last_checked_at=LATER, last_successful_fetch_at=LATER, source_update_date=LATER)
        self.assertEqual(r.content_hash, other.content_hash)
        self.assertNotEqual(r.metadata_hash, other.metadata_hash)

    def test_failed_target_carried_stale_healthy_target_continues(self):
        a = adapter([panel(1), panel(2)])
        first = extract_all(adapters=[a], now=FIXED_TIME)
        a.http.query.side_effect = [requests.HTTPError(response=Mock(status_code=404)), a.http.query.return_value]
        second = extract_all(first.records, adapters=[a], now=LATER)
        self.assertEqual(second.report["succeeded"], 1); self.assertEqual(second.report["failed"], 1)
        stale = [r for r in second.records.values() if r.metadata["stale"]]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].metadata["last_successful_fetch_at"], FIXED_TIME)
        self.assertEqual(stale[0].metadata["last_checked_at"], LATER)
        self.assertFalse(any(r.metadata["stale"] for r in first.records.values()))

    def test_all_failed_aborts_without_mutating_previous(self):
        a = adapter(); first = extract_all(adapters=[a], now=FIXED_TIME)
        before = canonical_json({k: r.to_dict() for k, r in first.records.items()})
        a.http.query.side_effect = requests.HTTPError(response=Mock(status_code=404))
        with self.assertRaises(ExtractionAborted): extract_all(first.records, adapters=[a], now=LATER)
        self.assertEqual(before, canonical_json({k: r.to_dict() for k, r in first.records.items()}))

    def test_transport_preserves_tls_timeout_and_no_redirect(self):
        session = Mock(); session.post.return_value.status_code = 200
        h = GrafanaHTTPClient(session=session, verify=True, timeout=17, min_interval=0)
        h.query("https://public.example.test/api/public/dashboards/x/panels/1/query", {},
                allowed_origin=origin(SOURCE["url"]))
        kw = session.post.call_args.kwargs
        self.assertIs(kw["verify"], True); self.assertEqual(kw["timeout"], 17)
        self.assertIs(kw["allow_redirects"], False)

    def test_transport_redirect_and_cross_origin_rejected(self):
        session = Mock(); session.post.return_value.status_code = 302
        h = GrafanaHTTPClient(session=session, verify=True, min_interval=0)
        with self.assertRaises(ValueError): h.query("https://public.example.test/q", {}, allowed_origin=origin(SOURCE["url"]))
        session.post.reset_mock()
        with self.assertRaises(ValueError): h.query("https://other.test/q", {}, allowed_origin=origin(SOURCE["url"]))
        session.post.assert_not_called()


if __name__ == "__main__": unittest.main()
