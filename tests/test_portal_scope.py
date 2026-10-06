import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests
from contracts import NormalizedRecord, SourceTarget, metadata_for
from delta_checker import DeltaChecker
from etl_common import atomic_write_json
from extractors.registry import configured_adapters, source_inventory
from main_orchestrator import run
from portal_scope import PortalScope, ScopedAdapters, source_key, fetch_scope, MENU_URL, SITE_CONFIG_URL
from transformers import build_documents, load_staging, validate_chunks
from tests.helpers import FakeAdapter, FIXED_TIME, LATER, http_error

def inventory(uuid="visible", name="Internal dashboard"):
    return {"source_type": "superset", "id": uuid, "name": name,
            "config": {"uuid": uuid, "name": name, "numeric_id": 1}}

def menu(uuid="visible", parent_status=1, page_status=1, name="Portal page", parent="Portal menu", identity=1):
    return {"id": identity, "name": parent, "status": parent_status, "children": [
        {"id": identity + 1, "name": name, "status": page_status,
         "source": "superset:https://dwh.jogjaprov.go.id|" + uuid, "children": []}]}

NO_HOME = {"data": [{"dashboard_url": None}]}

def record(uuid="visible", chart="1"):
    target = SourceTarget("superset", "1", chart, "https://dwh.jogjaprov.go.id/explore/?slice_id=" + chart,
                          "Internal chart " + chart, {"scope_id": uuid, "dashboard_name": "Internal dashboard"})
    meta = metadata_for(target, dashboard_uuid=uuid, last_checked_at=FIXED_TIME,
                        last_successful_fetch_at=FIXED_TIME, columns=["value"])
    return NormalizedRecord("superset", "1", chart, [{"value": 12}], meta, target.id).normalized()

class SourceFake(FakeAdapter):
    def __init__(self, records, **kwargs):
        super().__init__(records, **kwargs)
        self.source_type = "superset"
        self.adapter_id = records[0].target_id.rsplit(":", 1)[0]
    def discover(self):
        return [SourceTarget(r.source_type, r.dashboard_id, r.chart_id, r.metadata["source_url"],
                             r.metadata["chart_name"], {"scope_id": r.metadata["dashboard_uuid"],
                                                       "dashboard_name": r.metadata["dashboard_name"]})
                for r in self.records]

class ScopeTests(unittest.TestCase):
    def scope(self, menus=None, items=None, home=None):
        return PortalScope.from_payloads(items or [inventory()],
            menus if menus is not None else [menu()], home or NO_HOME)

    def test_only_active_pages_under_active_ancestors_selected(self):
        scope = self.scope([menu(), menu("hidden", parent_status=0, identity=3),
                            menu("inactive", page_status=0, identity=5)],
                           [inventory(), inventory("hidden"), inventory("inactive")])
        self.assertEqual([i["id"] for i in scope.selected], ["visible"])
        self.assertEqual(len(scope.summary()["excluded_sources"]), 2)

    def test_nested_hidden_parent_cannot_leak_child(self):
        branch = {"id": 1, "name": "Top", "status": 1, "children": [
            {"id": 2, "name": "Hidden", "status": 0, "children": [
                {"id": 3, "name": "Visible child", "status": 1,
                 "source": "superset:https://dwh.jogjaprov.go.id|visible"}]}]}
        self.assertEqual(self.scope([branch]).selected, [])

    def test_home_is_resolved_from_site_config_not_hardcoded(self):
        scope = self.scope([], [inventory("new-home")],
                           {"data": [{"dashboard_url": "superset:https://dwh.jogjaprov.go.id|new-home"}]})
        self.assertEqual(scope.visible_menu_names, [])
        self.assertEqual(scope.selected[0]["id"], "new-home")
        self.assertEqual(scope.matches(scope.selected[0])[0]["portal_page_name"], "Beranda")

    def test_exact_uuid_matching_not_names(self):
        self.assertEqual(self.scope([menu("different", name="Internal dashboard")]).selected, [])
        self.assertEqual(self.scope([menu("visible-suffix")]).selected, [])

    def test_cctv_and_tableau_display_query_mapping(self):
        cfg = {"superset": False, "cctv_groups": ["cctv-atcs"],
               "web_sources": [{"id": "students", "name": "Internal student source",
                                "url": "https://public.tableau.com/views/Students/Main?:showVizHome=no"}]}
        menus = [{"id": 1, "name": "Live", "status": 1, "children": [
            {"id": 2, "name": "Cctv Atcs", "status": 1, "source": "cctv.atcs"},
            {"id": 3, "name": "Students", "status": 1,
             "source": "https://public.tableau.com/views/Students/Main?:language=en-US&:display_count=n"}]}]
        scope = self.scope(menus, source_inventory(cfg))
        self.assertEqual(len(scope.selected), 2)
        self.assertEqual(len(scope.summary()["unmapped_visible_entries"]), 0)
        self.assertNotEqual(source_key(cfg["web_sources"][0]["url"]),
                            source_key("https://public.tableau.com/views/Students/Main?Year=2025"))
        self.assertNotEqual(source_key("https://lookerstudio.google.com/embed/reporting/report/page/a"),
                            source_key("https://lookerstudio.google.com/embed/reporting/report/page/b"))

    def test_unconfigured_visible_pages_reported_not_auto_scraped(self):
        scope = self.scope([menu("unknown")])
        self.assertEqual(scope.selected, [])
        self.assertEqual(len(scope.summary()["unmapped_visible_entries"]), 1)

    def test_duplicate_source_aliases_deterministic_without_duplicate_records(self):
        first = menu(name="Z page", identity=3)
        second = menu(name="A page", identity=1)
        a = self.scope([first, second])
        b = self.scope([second, first])
        self.assertEqual(a.summary(), b.summary())
        records, _ = a.apply({record().id: record()})
        self.assertEqual(len(records), 1)
        self.assertEqual(len(next(iter(records.values())).metadata["portal_entries"]), 2)

    def test_invalid_menu_and_site_config_fail_closed(self):
        cases = [({}, NO_HOME), ([{"id": 1, "name": "X"}], NO_HOME),
                 ([menu(), menu()], NO_HOME), ([menu()], {}),
                 ([menu()], {"data": [{}]}), ([menu()], {"data": [{"dashboard_url": 5}]})]
        for menus, config in cases:
            with self.subTest(menus=menus), self.assertRaises(ValueError):
                self.scope(menus, home=config if config else {"wrong": True})

    def test_portal_fields_and_internal_identity_preserved_on_every_chunk(self):
        raw = record()
        raw.raw_rows = [{"value": "x" * 150, "i": i} for i in range(8)]
        original = copy.deepcopy(raw.to_dict())
        records, excluded = self.scope().apply({raw.id: raw})
        self.assertEqual(excluded, [])
        self.assertEqual(raw.to_dict(), original)
        docs = build_documents(records, 500)
        self.assertGreater(len(docs), 1)
        self.assertTrue(validate_chunks(records, docs, 500))
        self.assertEqual(docs, build_documents(records, 500))
        for d in docs:
            m = d["metadata"]
            self.assertTrue(m["portal_visible"])
            self.assertEqual(m["portal_menu_name"], "Portal menu")
            self.assertEqual(m["portal_page_name"], "Portal page")
            self.assertEqual(m["source_dashboard_name"], "Internal dashboard")
            self.assertEqual(m["source_chart_name"], "Internal chart 1")
            self.assertEqual(m["record_id"], raw.id)
            self.assertEqual(m["dashboard_id"], raw.dashboard_id)
            self.assertIn("Portal page", d["page_content"])
            self.assertNotIn("Internal dashboard", d["page_content"])

    def test_portal_metadata_validation_and_hidden_chunk_guard(self):
        raw = record()
        raw.metadata["portal_visible"] = True
        with self.assertRaises(ValueError):
            raw.validate()
        records, _ = self.scope().apply({record().id: record()})
        records[record().id].metadata["portal_visible"] = False
        with self.assertRaisesRegex(ValueError, "Hidden portal record"):
            build_documents(records)

    def test_portal_name_change_affects_content_hash_but_not_identity(self):
        raw = record()
        a, _ = self.scope().apply({raw.id: raw})
        b, _ = self.scope([menu(name="Renamed portal page")]).apply({raw.id: raw})
        self.assertEqual(next(iter(a)), next(iter(b)))
        self.assertNotEqual(a[raw.id].content_hash, b[raw.id].content_hash)

    def test_fetches_menu_and_site_config_using_shared_http(self):
        http = Mock()
        http.get.side_effect = [Mock(json=lambda: [menu()]), Mock(json=lambda: NO_HOME)]
        scope = fetch_scope([inventory()], http)
        self.assertEqual(len(scope.selected), 1)
        self.assertEqual([c.args[0] for c in http.get.call_args_list], [MENU_URL, SITE_CONFIG_URL])

    def test_registry_does_not_construct_or_discover_hidden_adapters(self):
        scope = self.scope([menu(), menu("hidden", parent_status=0, identity=3)],
                           [inventory(), inventory("hidden")])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text("{}")
            with patch("portal_scope.fetch_scope", return_value=scope), \
                 patch("extractors.registry.SupersetAdapter") as constructor:
                adapters = configured_adapters(path)
                constructor.assert_called_once_with(scope.selected[0]["config"])
                self.assertEqual(len(adapters), 1)
                self.assertIs(adapters.portal_scope, scope)

class ScopePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def scope(self, visible=True, name="Portal page"):
        return PortalScope.from_payloads([inventory(), inventory("hidden")],
            [menu(page_status=int(visible), name=name), menu("hidden", parent_status=0, identity=3)], NO_HOME)
    def run_scope(self, scope=None, adapter=None):
        scoped = ScopedAdapters([adapter or SourceFake([record()])] if (scope or self.scope()).selected else [],
                                scope or self.scope())
        return run(adapters=scoped, output_dir=self.path, now=LATER)
    def seed(self):
        self.assertEqual(run(adapters=[SourceFake([record(), record("hidden")])],
                             output_dir=self.path, now=FIXED_TIME), 0)
    def artifacts(self):
        return {n: (self.path / n).read_bytes() for n in
                ("staging_idmc_data.json", "ready_for_vector_db_v2.json", "delta_state_v2.json")}
    def report(self):
        return json.loads((self.path / "extraction_report_v2.json").read_text())

    def test_hidden_previous_records_removed_not_carried_forward(self):
        self.seed()
        self.assertEqual(self.run_scope(), 0)
        rows = load_staging(self.path / "staging_idmc_data.json")
        self.assertEqual(set(rows), {record().id})
        self.assertEqual(self.report()["portal_scope"]["previous_records_excluded"], 1)
        state = json.loads((self.path / "delta_state_v2.json").read_text())
        self.assertNotIn(record("hidden").id, state["content_hashes"])
        docs = json.loads((self.path / "ready_for_vector_db_v2.json").read_text())
        self.assertTrue(all(d["metadata"]["portal_visible"] for d in docs))

    def test_same_scope_input_and_time_produce_identical_artifact_bytes(self):
        self.assertEqual(self.run_scope(), 0)
        before = self.artifacts()
        self.assertEqual(self.run_scope(), 0)
        self.assertEqual(before, self.artifacts())
        self.assertEqual(self.report()["delta_reason"], "unchanged")

    def test_hidden_source_is_selected_again_when_parent_becomes_visible(self):
        scope = PortalScope.from_payloads([inventory(), inventory("hidden")],
            [menu(), menu("hidden", identity=3)], NO_HOME)
        self.assertEqual(len(scope.selected), 2)
        self.assertEqual(self.run_scope(scope, SourceFake([record(), record("hidden")])), 0)
        self.assertEqual(set(load_staging(self.path / "staging_idmc_data.json")),
                         {record().id, record("hidden").id})

    def test_visible_failure_stays_stale_with_current_portal_name(self):
        one, two = record(), record(chart="2")
        self.assertEqual(run(adapters=[SourceFake([one, two])], output_dir=self.path, now=FIXED_TIME), 0)
        scope = self.scope(name="New portal name")
        self.assertEqual(self.run_scope(scope, SourceFake([one, two], failures={"1": http_error(404)})), 2)
        restored = load_staging(self.path / "staging_idmc_data.json")[one.id]
        self.assertTrue(restored.metadata["stale"])
        self.assertEqual(restored.metadata["last_successful_fetch_at"], FIXED_TIME)
        self.assertEqual(restored.metadata["portal_page_name"], "New portal name")

    def test_menu_fetch_failure_preserves_published_files_and_skips_sources(self):
        self.seed()
        before = self.artifacts()
        with patch("main_orchestrator.startup_check"), \
             patch("portal_scope.fetch_scope", side_effect=requests.Timeout()), \
             patch("extractors.registry.SupersetAdapter") as adapter:
            self.assertEqual(run(output_dir=self.path, now=LATER), 1)
            adapter.assert_not_called()
        self.assertEqual(before, self.artifacts())
        self.assertEqual(self.report()["portal_scope"]["status"], "unavailable")

    def test_verified_all_hidden_scope_publishes_empty_removing_old_content(self):
        self.seed()
        self.assertEqual(self.run_scope(self.scope(visible=False)), 0)
        self.assertEqual(load_staging(self.path / "staging_idmc_data.json"), {})
        self.assertEqual(json.loads((self.path / "ready_for_vector_db_v2.json").read_text()), [])
        self.assertEqual(self.report()["portal_scope"]["previous_records_excluded"], 2)

    def test_unmapped_visible_entry_produces_partial_report(self):
        scope = PortalScope.from_payloads([inventory()], [menu(), menu("unconfigured", identity=3)], NO_HOME)
        self.assertEqual(self.run_scope(scope), 2)
        self.assertEqual(len(self.report()["portal_scope"]["unmapped_visible_entries"]), 1)

    def test_standalone_output_contains_only_visible_chunks(self):
        self.seed()
        adapters = ScopedAdapters([SourceFake([record()])], self.scope())
        self.assertEqual(run(adapters=adapters, output_dir=self.path, now=LATER), 0)
        docs = json.loads((self.path / "ready_for_vector_db_v2.json").read_text())
        self.assertTrue(docs)
        self.assertTrue(all(d["metadata"]["portal_visible"] for d in docs))
        self.assertEqual({d["metadata"]["record_id"] for d in docs}, {record().id})

    def test_scoped_publication_failure_still_rolls_back(self):
        self.seed()
        before = self.artifacts()
        with patch("main_orchestrator.publish_bundle", side_effect=OSError("disk failure")):
            self.assertEqual(self.run_scope(), 1)
        self.assertEqual(before, self.artifacts())
