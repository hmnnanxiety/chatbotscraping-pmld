import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from extractors.looker_studio import LookerStudioAdapter, LookerExtractionError, collect_table, report_identity
from extractors.looker_browser import LookerBrowser, materialize_page
from extractors.registry import source_inventory, configured_adapters
from extractors.cctv import CCTVNativeAdapter
from extractor import classify_error
from extractors.base import retry_source, transient_error
from tenacity import wait_none
from main_orchestrator import run
from portal_scope import PortalScope, ScopedAdapters
from transformers import build_documents, load_staging, validate_chunks
from tests.helpers import FIXED_TIME, LATER
from tests.looker_fixtures import FixtureBrowser, FIXTURE_ROWS, table_html

ROOT = Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT/"ingestion_sources.json").read_text(encoding="utf-8-sig"))


def page(values=None,first=1,last=2,total=2):
    return {"columns":["value"],"rows":values if values is not None else [["same"],["same"]],
            "row_start":first,"row_end":last,"total_rows":total}


def extract_fixture(source):
    adapter=LookerStudioAdapter(source,FixtureBrowser)
    with patch("contracts.utcnow",return_value=FIXED_TIME):
        target=adapter.discover()[0]
        record=adapter.extract(target)[0]
    return record


class LookerTests(unittest.TestCase):
    def setUp(self):
        self.sources=config()["looker_sources"]
    def test_five_reports_normalized_with_stable_ids_and_real_rows_not_metadata(self):
        self.assertEqual(len(self.sources),5)
        records={}
        for source in self.sources:
            with self.subTest(source=source["id"]):
                record=extract_fixture(source)
                self.assertEqual(len(record.raw_rows),FIXTURE_ROWS[source["id"]])
                self.assertEqual(record.dashboard_id,report_identity(source["url"])[0])
                self.assertEqual(record.metadata["report_page_id"],report_identity(source["url"])[1])
                self.assertEqual(record.metadata["extraction_mode"],"browser_table")
                self.assertTrue(record.metadata["is_complete"])
                self.assertEqual(record.to_dict(),extract_fixture(source).to_dict())
                records[record.id]=record
        docs=build_documents(records,500)
        self.assertEqual(docs,build_documents(records,500))
        self.assertTrue(validate_chunks(records,docs,500))
        self.assertEqual(sum(len(r.raw_rows) for r in records.values()),40)
    def test_equal_data_rows_not_deduplicated(self):
        rows,_,_=collect_table([page()])
        self.assertEqual(rows,[{"value":"same"},{"value":"same"}])
    def test_pagination_preserves_row_positions(self):
        rows,_,count=collect_table([page([["a"]],1,1,3),page([["b"],["c"]],2,3,3)])
        self.assertEqual([r["value"] for r in rows],["a","b","c"])
        self.assertEqual(count,2)
    def test_explicit_empty_table_is_not_metadata_fallback(self):
        self.assertEqual(collect_table([page([],0,0,0)])[0],[])
    def test_invalid_ranges_totals_schema_and_rows_fail(self):
        bad=[]
        for key,value in [("row_start",0),("row_end",3),("total_rows",3),("columns",["x","x"]),
                          ("columns",[""]),("rows",[[1],[2]]),("rows",[["a"]])]:
            b=page();b[key]=value;bad.append([b])
        bad.extend([[page(),page()], [page(total=3),page([["z"]],3,3,4)],
                    [page(total=3),dict(page([["z"]],3,3,3),columns=["different"])]])
        for values in bad:
            with self.subTest(values=values),self.assertRaises(LookerExtractionError):
                collect_table(values)
    def test_bounds_enforced(self):
        with self.assertRaises(LookerExtractionError):collect_table([page()],max_rows=1)
        with self.assertRaises(LookerExtractionError):collect_table([page(),page()],max_pages=1)
    def test_failed_later_page_returns_no_partial_rows(self):
        def pages():
            yield page(total=3)
            raise LookerExtractionError("Second page unavailable")
        with self.assertRaises(LookerExtractionError):collect_table(pages())
    def test_mixed_supported_and_unsupported_chart_is_reported_partial(self):
        class Mixed(FixtureBrowser):
            def discover(self):return super().discover()+[{"component_id":"cd-score","kind":"unsupported"}]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(adapters=[LookerStudioAdapter(self.sources[0],Mixed)],output_dir=tmp,now=FIXED_TIME),2)
            report=json.loads((Path(tmp)/"extraction_report_v2.json").read_text())
            self.assertEqual(report["succeeded"],1)
            self.assertEqual(report["skipped"],1)
            self.assertTrue(report["skipped_targets"][0]["reason"].startswith("unsupported:"))
            self.assertEqual(report["metadata_only"],0)
    def test_registry_uses_one_reusable_looker_adapter_for_all_five(self):
        cfg=config();items=[i for i in source_inventory(cfg) if i["source_type"]=="looker_studio"]
        menus=[{"id":i+1,"name":item["name"],"status":1,"source":item["config"]["url"]}
               for i,item in enumerate(items)]
        scope=PortalScope.from_payloads(items,menus,{"data":[{"dashboard_url":None}]})
        with patch("portal_scope.fetch_scope",return_value=scope), \
             patch("extractors.registry.LookerStudioAdapter",side_effect=lambda source:LookerStudioAdapter(source,FixtureBrowser)) as ctor:
            adapters=configured_adapters(ROOT/"ingestion_sources.json")
        self.assertEqual(ctor.call_count,5)
        self.assertEqual(len(adapters),5)
        records={r.id:r for source in self.sources for r in [extract_fixture(source)]}
        records,excluded=scope.apply(records)
        self.assertEqual(excluded,[])
        self.assertTrue(all(r.metadata["portal_visible"] for r in records.values()))
        self.assertTrue(validate_chunks(records,build_documents(records)))
    def test_report_urls_reject_other_hosts_extra_filters_and_credentials(self):
        for url in ["https://evil.test/reporting/id","https://user@lookerstudio.google.com/reporting/id",
                    "http://lookerstudio.google.com/reporting/id",
                    "https://lookerstudio.google.com:8443/reporting/id",
                    "https://lookerstudio.google.com/reporting/id?Year=2025"]:
            with self.subTest(url=url),self.assertRaises(LookerExtractionError):report_identity(url)
    def test_unsupported_report_fails_not_metadata_success(self):
        browser=Mock();browser.__enter__=Mock(return_value=browser);browser.__exit__=Mock(return_value=False)
        browser.discover.return_value=[{"component_id":"cd-a","kind":"scorecard"}]
        with self.assertRaises(LookerExtractionError) as caught:
            LookerStudioAdapter(self.sources[0],lambda s:browser).discover()
        self.assertEqual(classify_error(caught.exception),"unsupported")
    def test_duplicate_component_ids_rejected(self):
        class Duplicate(FixtureBrowser):
            def discover(self):return super().discover()*2
        with self.assertRaises(LookerExtractionError):LookerStudioAdapter(self.sources[0],Duplicate).discover()
    def test_page_escape_rejected(self):
        class Escaped(FixtureBrowser):
            def discover(self):return [dict(super().discover()[0],page_id="wrong-page")]
        with self.assertRaises(LookerExtractionError):LookerStudioAdapter(self.sources[0],Escaped).discover()
    def test_runtime_error_categories_explicit(self):
        for category in ("unsupported","browser_unavailable","validation","timeout"):
            self.assertEqual(classify_error(LookerExtractionError("safe",category)),category)
    def test_browser_timeout_retries_without_retrying_permanent_failures(self):
        operation=Mock(side_effect=[LookerExtractionError("Read timed out","timeout"),"ok"])
        wrapped=retry_source(operation).retry_with(wait=wait_none())
        self.assertEqual(wrapped(),"ok")
        self.assertEqual(operation.call_count,2)
        for category in ("validation","unsupported","browser_unavailable"):
            self.assertFalse(transient_error(LookerExtractionError("safe",category)))
    def test_virtualized_rows_reassembled_and_unlabelled_ordinals_removed(self):
        snapshot={"columns":["","ID"],"range":[1,2,2]}
        result=materialize_page(snapshot,{1:["2.","b"],0:["1.","a"]})
        self.assertEqual(result["rows"],[["a"],["b"]])
        self.assertEqual(result["columns"],["ID"])
        with self.assertRaises(LookerExtractionError):materialize_page(snapshot,{0:["1.","a"]})
        with self.assertRaises(LookerExtractionError):materialize_page(snapshot,{0:["manual","a"],1:["2.","b"]})
        with self.assertRaises(LookerExtractionError):
            materialize_page(dict(snapshot,range=[3,4,4]),{0:["1.","a"],1:["2.","b"]})
    def test_native_block_slot_coverage_preserves_equal_rows_and_order(self):
        snapshot={"columns":["Value"],"range":[21,24,24]}
        rows={(1,1):["same"],(0,0):["first"],(1,0):["same"],(0,1):["second"]}
        self.assertEqual(materialize_page(snapshot,rows)["rows"],[["first"],["second"],["same"],["same"]])
        for keys in ({(0,0):["a"],(2,0):["b"]}, {(0,0):["a"],(0,2):["b"]}, {0:["a"],(0,1):["b"]}):
            with self.assertRaises(LookerExtractionError):materialize_page(dict(snapshot,range=[1,2,2]),keys)
    def test_failed_lookers_carry_stale_without_refreshing_successful_fetch(self):
        class Broken(FixtureBrowser):
            def pages(self,cid):raise LookerExtractionError("Invalid coverage")
        with tempfile.TemporaryDirectory() as tmp:
            adapters=[LookerStudioAdapter(s,FixtureBrowser) for s in self.sources]
            self.assertEqual(run(adapters=adapters,output_dir=tmp,now=FIXED_TIME),0)
            old=load_staging(Path(tmp)/"staging_idmc_data.json")
            adapters[0]=LookerStudioAdapter(self.sources[0],Broken)
            self.assertEqual(run(adapters=adapters,output_dir=tmp,now=LATER),2)
            restored=load_staging(Path(tmp)/"staging_idmc_data.json")
            key=extract_fixture(self.sources[0]).id
            self.assertTrue(restored[key].metadata["stale"])
            self.assertEqual(restored[key].metadata["last_successful_fetch_at"],old[key].metadata["last_successful_fetch_at"])
            report=json.loads((Path(tmp)/"extraction_report_v2.json").read_text())
            self.assertEqual(report["metadata_only"],0)
            before=(Path(tmp)/"staging_idmc_data.json").read_bytes()
            self.assertEqual(run(adapters=[LookerStudioAdapter(s,Broken) for s in self.sources],output_dir=tmp,now=LATER),1)
            self.assertEqual(before,(Path(tmp)/"staging_idmc_data.json").read_bytes())


class KotaTests(unittest.TestCase):
    def test_alias_and_273_camera_pagination_preserve_provenance(self):
        cfg=config(); inv=[i for i in source_inventory(cfg) if i["id"]=="cctv-kota"]
        menus=[{"id":1,"name":"Dashboard Surveillance","status":1,"children":[
            {"id":2,"name":"cctv kota","status":1,"source":"cctv.atcs-kota"}]}]
        scope=PortalScope.from_payloads(inv,menus,{"data":[{"dashboard_url":None}]})
        self.assertEqual(len(scope.selected),1)
        self.assertEqual(scope.summary()["unmapped_visible_entries"],[])
        http=Mock();adapter=CCTVNativeAdapter(["cctv-kota"],http=http);target=adapter.discover()[0]
        responses=[]
        for start,end in [(0,100),(100,200),(200,273)]:
            payload={"data":[{"id":i,"location":"cctv-kota"} for i in range(start,end)],
                     "meta":{"total":273},"links":{"next":target.source_url+f"&page={end//100+1}" if end<273 else None}}
            responses.append(Mock(json=Mock(return_value=payload)))
        http.get.side_effect=responses
        record=adapter.extract(target)[0]
        records,_=scope.apply({record.id:record});record=records[record.id]
        self.assertEqual(len(record.raw_rows),273)
        self.assertEqual(http.get.call_count,3)
        self.assertIn("cctv-kota",target.source_url)
        self.assertEqual(record.dashboard_id,"cctv-kota")
        self.assertEqual(record.metadata["portal_page_name"],"cctv kota")
        self.assertTrue(validate_chunks(records,build_documents(records)))


class OfflineDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("playwright") is None:
            raise unittest.SkipTest("Optional Playwright DOM integration dependency not installed")
    def session(self,source,virtualized,hidden=False,buffered=False,blocks=False):
        class LocalBrowser(LookerBrowser):
            def __enter__(self):
                from playwright.sync_api import sync_playwright,TimeoutError,Error
                self.TimeoutError,self.BrowserError=TimeoutError,Error
                self.driver=sync_playwright().start()
                edge=Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
                self.browser=self.driver.chromium.launch(headless=True,**({"channel":"msedge"} if edge.exists() else {}))
                self.context=self.browser.new_context(ignore_https_errors=False)
                self.context.route("**/*",lambda route:route.abort())
                self.page=self.context.new_page();self.page.set_default_timeout(3000)
                self.resolved_page=report_identity(source["url"])[1]
                html=table_html(virtualized,buffered)
                if blocks:
                    html=html.replace('<div class="headerCell"><div class="colName"></div></div>','')
                    html=html.replace('<div class="cell">${first+i+1}.</div>','')
                    html=html.replace('index-${first+i}','block-${Math.floor((first+i)/8)} index-${(first+i)%8}')
                if hidden:
                    html=html.replace('overflow-y:auto','overflow-y:hidden')
                    html=html.replace('index-${first+i}', 'index-${i}')
                self.page.set_content(html)
                return self
        return LocalBrowser(source,timeout_ms=3000)
    def test_real_dom_parser_pagination_without_network(self):
        adapter=LookerStudioAdapter(config()["looker_sources"][0],lambda s:self.session(s,False))
        record=adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(len(record.raw_rows),10)
        self.assertEqual(record.metadata["pages_fetched"],4)
        self.assertEqual(record.metadata["columns"],["ID","Label"])
    def test_real_dom_parser_virtualization_without_network(self):
        adapter=LookerStudioAdapter(config()["looker_sources"][0],lambda s:self.session(s,True))
        record=adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(len(record.raw_rows),10)
        self.assertEqual(len({r["ID"] for r in record.raw_rows}),10)
    def test_real_dom_parser_hidden_overflow_scroll_without_network(self):
        adapter=LookerStudioAdapter(config()["looker_sources"][0],lambda s:self.session(s,True,True))
        record=adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(len(record.raw_rows),10)
        self.assertEqual(len({r["ID"] for r in record.raw_rows}),10)
    def test_real_dom_parser_buffered_recycled_slots_without_network(self):
        adapter=LookerStudioAdapter(config()["looker_sources"][0],lambda s:self.session(s,True,True,True))
        record=adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(len(record.raw_rows),20)
        self.assertEqual(len({r["ID"] for r in record.raw_rows}),20)
    def test_real_dom_parser_native_blocks_without_ordinals(self):
        adapter=LookerStudioAdapter(config()["looker_sources"][0],lambda s:self.session(s,True,True,True,True))
        record=adapter.extract(adapter.discover()[0])[0]
        self.assertEqual(len(record.raw_rows),20)
        self.assertEqual(len({r["ID"] for r in record.raw_rows}),20)
        self.assertEqual(record.metadata["columns"],["ID","Label"])
    def test_headerless_accessibility_table_is_not_discovered_as_supported(self):
        source=config()["looker_sources"][0]
        with self.session(source,False) as browser:
            browser.page.set_content('<div class="lego-component cd-headerless" style="width:500px;height:200px"><table><tbody><tr><td>a</td><td>b</td></tr></tbody></table></div>')
            self.assertEqual(browser.discover()[0]["kind"],"unsupported")


if __name__=="__main__":unittest.main()
