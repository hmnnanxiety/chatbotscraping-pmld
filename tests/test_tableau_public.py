"""Synthetic visible-layout fixtures only; no production bootstrap/session data."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from extractors.tableau_public import TableauPublicAdapter, TableauExtractionError, decode_bootstrap, visible_cards, view_identity
from extractors.tableau_browser import TableauBrowser
from extractors.registry import source_inventory, configured_adapters
from extractor import classify_error, extract_all, ExtractionAborted
from main_orchestrator import run
from portal_scope import PortalScope, ScopedAdapters
from tests.helpers import FIXED_TIME, LATER
from transformers import build_documents, validate_chunks, load_staging

ROOT = Path(__file__).resolve().parents[1]


def source():
    return json.loads((ROOT / "ingestion_sources.json").read_text(encoding="utf-8-sig"))["tableau_sources"][0]


def payload():
    zones = {}
    for n, label in enumerate(sorted(source()["visible_metrics"])):
        zones[str(n)] = {"isVisible": True, "zoneCommon": {"zoneType": "text"},
                         "presModelHolder": {"dashboardText": {"caption": label + "\n" + str((n+1)*10) + ".250 m"}}}
    zones["date"] = {"isVisible": True, "presModelHolder": {"visual": {"visualTitle": {"caption": "Last Update : synthetic source date"}}}}
    zones["publisher"] = {"isVisible": True, "zoneCommon": {"zoneType": "text"},
                          "presModelHolder": {"dashboardText": {"caption": "Data Source : Synthetic publisher"}}}
    return {"sheetName": "FO", "worldUpdate": {"applicationPresModel": {"workbookPresModel": {"dashboardPresModel": {"zones": zones}}}}}


def zones(p):
    return p["worldUpdate"]["applicationPresModel"]["workbookPresModel"]["dashboardPresModel"]["zones"]


class FixtureBrowser:
    def __init__(self, config): self.config = config
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def snapshot(self):
        return {"view": "FO", "worksheets": ["Synthetic map", "Synthetic update"],
                **visible_cards([payload()], "FO", self.config["visible_metrics"])}


def scope():
    cfg = {"superset": False, "tableau_sources": [source()]}
    return PortalScope.from_payloads(source_inventory(cfg), [
        {"id": 170, "name": "Dashboard Ajimandaya", "status": 1, "children": [
            {"id": 171, "name": "Jaringan di DIY", "status": 1, "source": source()["url"]}]}],
        {"data": [{"dashboard_url": None}]})


class TableauTests(unittest.TestCase):
    def test_identity_and_config_exact_portal_mapping(self):
        self.assertEqual(view_identity(source()["url"]), ("DashboardJaringanDIY", "FO"))
        self.assertEqual(len(scope().selected), 1)
        self.assertEqual(scope().summary()["unmapped_visible_entries"], [])

    def test_reject_wrong_host_credentials_filters_and_path(self):
        for u in ["http://public.tableau.com/views/a/b", "https://evil.test/views/a/b",
                  "https://user@public.tableau.com/views/a/b", "https://public.tableau.com/views/a/b?Kabupaten=Bantul",
                  "https://public.tableau.com/views/a/b#other", "https://public.tableau.com:8443/views/a/b"]:
            with self.subTest(url=u), self.assertRaises(TableauExtractionError): view_identity(u)

    def test_utf16_framing_unicode_and_multiple_frames(self):
        values = [{"caption": "données \U0001f30f"}, {"done": True}]
        encoded = [json.dumps(v, ensure_ascii=False) for v in values]
        body = "".join(str(len(v.encode("utf-16-le"))//2) + ";" + v for v in encoded).encode()
        self.assertEqual(decode_bootstrap(body), values)

    def test_malformed_truncated_empty_and_oversized_frames_fail(self):
        for body in (b"", b"{}", b"99;{}", b"2;{x", b"2;{}garbage", b"0;", b"x"*5_000_001):
            with self.subTest(body=body[:10]), self.assertRaises(TableauExtractionError): decode_bootstrap(body)

    def test_real_rows_and_source_reported_date_not_metadata_fallback(self):
        adapter = TableauPublicAdapter(source(), FixtureBrowser)
        targets = adapter.discover()
        with patch("contracts.utcnow", return_value=FIXED_TIME): record = adapter.extract(targets[0])[0]
        self.assertEqual(len(record.raw_rows), 3)
        self.assertEqual(set(record.raw_rows[0]), {"metric", "display_value", "unit"})
        self.assertEqual(record.metadata["source_update_date"], "synthetic source date")
        self.assertEqual(record.metadata["statistical_period"], None)
        self.assertEqual(record.metadata["extraction_mode"], "visible_metric_cards")
        self.assertFalse(record.metadata["is_complete"])
        self.assertTrue(record.metadata["metric_cards_complete"])
        self.assertIn("publisher-authored", record.metadata["data_semantics"])

    def test_wrong_view_missing_layout_and_empty_metrics_fail(self):
        p = payload(); p["sheetName"] = "Other"
        for frames in ([p], [{"sheetName": "FO"}], []):
            with self.assertRaises(TableauExtractionError): visible_cards(frames, "FO", source()["visible_metrics"])

    def test_duplicate_missing_hidden_cards_fail_closed(self):
        for mode in ("duplicate", "missing", "hidden"):
            p = payload(); z = zones(p)
            if mode == "duplicate": z["clone"] = copy.deepcopy(z["0"])
            elif mode == "missing": del z["0"]
            else: z["0"]["isVisible"] = False
            with self.subTest(mode=mode), self.assertRaises(TableauExtractionError):
                visible_cards([p], "FO", source()["visible_metrics"])

    def test_invalid_value_or_unit_no_empty_success(self):
        for text in ("", "unknown m", "10 km", "10 m\nextra", "1..2 m"):
            p = payload(); label = sorted(source()["visible_metrics"])[0]
            zones(p)["0"]["presModelHolder"]["dashboardText"]["caption"] = label + "\n" + text
            with self.subTest(text=text), self.assertRaises(TableauExtractionError):
                visible_cards([p], "FO", source()["visible_metrics"])

    def test_hidden_duplicate_is_ignored(self):
        p = payload(); z = zones(p); z["clone"] = copy.deepcopy(z["0"]); z["clone"]["isVisible"] = False
        self.assertEqual(len(visible_cards([p], "FO", source()["visible_metrics"])["cards"]), 3)

    def test_visible_zone_worksheet_identity_not_hidden_workbook_tab(self):
        p = payload()
        zones(p)["map"] = {"isVisible": True, "presModelHolder": {"visual": {
            "visualIdPresModel": {"worksheet": "Hidden workbook tab with visible map", "dashboard": "FO"}}}}
        zones(p)["hidden"] = {"isVisible": False, "presModelHolder": {"visual": {
            "visualIdPresModel": {"worksheet": "Hidden zone", "dashboard": "FO"}}}}
        self.assertEqual(visible_cards([p], "FO", source()["visible_metrics"])["visible_worksheets"],
                         ["Hidden workbook tab with visible map"])

    def test_browser_cross_checks_transmitted_cards_against_visible_dom(self):
        p = payload()
        zones(p)["map"] = {"isVisible": True, "presModelHolder": {"visual": {
            "visualIdPresModel": {"worksheet": "Visible map", "dashboard": "FO"}}}}
        browser = TableauBrowser(source())
        browser.frames = [p]
        browser.page = Mock()
        browser.page.evaluate.return_value = {"view": "FO", "worksheets": ["Visible map", "Other hidden zone"]}
        frame = Mock(); frame.url = "https://public.tableau.com/views/DashboardJaringanDIY/FO"
        captions = [z["presModelHolder"].get("dashboardText", {}).get("caption", "") for z in zones(p).values()]
        frame.locator.return_value.inner_text.return_value = "\n".join(captions)
        browser.page.frames = [frame]
        self.assertEqual(browser.snapshot()["worksheets"], ["Visible map"])
        frame.locator.return_value.inner_text.return_value = "loading or unrelated page"
        with self.assertRaises(TableauExtractionError): browser.snapshot()

    def test_public_bootstrap_session_identifiers_never_enter_output(self):
        class SessionBrowser(FixtureBrowser):
            def snapshot(self):
                p = payload(); p["newSessionId"] = "DO_NOT_PERSIST_SESSION"
                return {"view": "FO", "worksheets": ["Synthetic map"],
                        **visible_cards([p], "FO", self.config["visible_metrics"])}
        adapter = TableauPublicAdapter(source(), SessionBrowser)
        record = adapter.extract(adapter.discover()[0])[0]
        self.assertNotIn("DO_NOT_PERSIST_SESSION", json.dumps(record.to_dict()))

    def test_conflicting_source_update_dates_fail(self):
        p = payload(); zones(p)["conflict"] = {"isVisible": True, "presModelHolder": {"dashboardText": {"caption": "Last Update : different"}}}
        with self.assertRaises(TableauExtractionError): visible_cards([p], "FO", source()["visible_metrics"])

    def test_worksheet_data_explicitly_unsupported_even_when_forced(self):
        adapter = TableauPublicAdapter(source(), FixtureBrowser); targets = adapter.discover()
        self.assertEqual(len(targets), 3)
        for target in targets[1:]:
            self.assertIn("unsupported:", target.options["skip_reason"])
            with self.assertRaises(TableauExtractionError) as error: adapter.extract(target)
            self.assertEqual(classify_error(error.exception), "unsupported")

    def test_provenance_and_deterministic_chunks(self):
        adapter = TableauPublicAdapter(source(), FixtureBrowser)
        with patch("contracts.utcnow", return_value=FIXED_TIME): r = adapter.extract(adapter.discover()[0])[0]
        records, excluded = scope().apply({r.id: r})
        self.assertEqual(excluded, [])
        docs = build_documents(records)
        self.assertEqual(docs, build_documents(records))
        self.assertTrue(validate_chunks(records, docs))
        self.assertEqual(len(docs), 1)
        m = docs[0]["metadata"]
        self.assertEqual(m["portal_page_name"], "Jaringan di DIY")
        self.assertEqual(m["portal_menu_name"], "Dashboard Ajimandaya")
        self.assertEqual((m["workbook"],m["view"]), ("DashboardJaringanDIY","FO"))

    def test_registry_reuses_adapter_without_changing_legacy_web_sources(self):
        with patch("portal_scope.fetch_scope", return_value=scope()): adapters = configured_adapters(ROOT / "ingestion_sources.json")
        self.assertEqual(len(adapters), 1)
        self.assertIsInstance(adapters[0], TableauPublicAdapter)

    def test_failed_discovery_does_not_reuse_cached_success(self):
        adapter = TableauPublicAdapter(source(), FixtureBrowser); adapter.discover()
        with patch.object(adapter, "_fetch", side_effect=TableauExtractionError("changed viewer")):
            with self.assertRaises(TableauExtractionError): adapter.discover()
        self.assertIsNone(adapter._snapshot)

    def test_partial_report_and_failed_run_preserve_good_state(self):
        class Broken(FixtureBrowser):
            def snapshot(self): raise TableauExtractionError("Cards unavailable", "unsupported")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            adapter = TableauPublicAdapter(source(), FixtureBrowser)
            self.assertEqual(run(adapters=ScopedAdapters([adapter], scope()), output_dir=path, now=FIXED_TIME), 2)
            report = json.loads((path / "extraction_report_v2.json").read_text())
            self.assertEqual((report["records_produced"],report["metadata_only"],report["skipped"]), (1,0,2))
            names = ("staging_idmc_data.json","ready_for_vector_db_v2.json","delta_state_v2.json")
            before = {n:(path/n).read_bytes() for n in names}
            old = load_staging(path / names[0])
            with self.assertRaises(ExtractionAborted):
                extract_all(old, adapters=[TableauPublicAdapter(source(), Broken)], now=LATER)
            self.assertEqual(run(adapters=ScopedAdapters([TableauPublicAdapter(source(), Broken)], scope()), output_dir=path, now=LATER), 1)
            self.assertEqual(before, {n:(path/n).read_bytes() for n in names})


if __name__ == "__main__": unittest.main()
