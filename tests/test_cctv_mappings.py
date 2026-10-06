"""Offline regression coverage for the nine verified CONFIG_ONLY CCTV groups."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from extractors.cctv import CCTVNativeAdapter
from extractors.registry import configured_adapters, source_inventory
from portal_scope import PortalScope, ScopedAdapters
from tests.helpers import FIXED_TIME
from transformers import build_documents, validate_chunks

ROOT = Path(__file__).resolve().parents[1]
VERIFIED = (
    "cctv-bantul", "cctv-kominfosleman", "cctv-kominfogk", "cctv-kp",
    "cctv-public", "cctv-sleman", "cctv-spl", "cctv-sungai", "cctv-uptmalioboro",
)


def config():
    return json.loads((ROOT / "ingestion_sources.json").read_text(encoding="utf-8-sig"))


def scope(active=True):
    cfg = {"superset": False, "cctv_groups": config()["cctv_groups"], "web_sources": [],
           "cctv_portal_aliases": config()["cctv_portal_aliases"]}
    groups = ("cctv-atcs",) + VERIFIED + ("cctv-atcs-kota",)
    children = [{"id": i + 2, "name": group, "status": 1,
                 "source": "cctv." + group.removeprefix("cctv-")} for i, group in enumerate(groups)]
    menus = [{"id": 1, "name": "Dashboard Surveillance", "status": int(active), "children": children}]
    # Minimal public-source fixture: intentionally unconfigured platforms/pages.
    others = [
        "https://lookerstudio.google.com/embed/reporting/ikm/page/a",
        "https://public.tableau.com/views/Jaringan/FO",
        "https://lookerstudio.google.com/embed/reporting/spbe2023/page/a",
        "https://lookerstudio.google.com/embed/reporting/spbe2024",
        "https://lookerstudio.google.com/embed/reporting/spbe2025/page/a",
        "https://lookerstudio.google.com/embed/reporting/masterplan/page/a",
        "https://xplore.pustakadata.id/public-dashboards/yogyakarta",
        "https://xplore.pustakadata.id/public-dashboards/mudik",
    ]
    menus.extend({"id": i + 20, "name": "Unconfigured " + str(i), "status": 1, "source": url}
                 for i, url in enumerate(others))
    return PortalScope.from_payloads(source_inventory(cfg), menus, {"data": [{"dashboard_url": None}]})


class VerifiedCCTVMappingTests(unittest.TestCase):
    def test_config_keeps_nine_verified_groups_and_adds_kota_alias(self):
        cfg = config()
        self.assertEqual(cfg["cctv_groups"], ["cctv-atcs", *VERIFIED, "cctv-kota"])
        self.assertEqual(len(set(cfg["cctv_groups"])), 11)
        self.assertTrue(cfg["superset"])
        self.assertEqual([s["id"] for s in cfg["web_sources"]],
                         ["tableau-pelajar-diy", "looker-perkebunan-diy"])

    def test_kota_alias_maps_actual_location_but_other_unconfigured_sources_remain_unmapped(self):
        selected = scope()
        self.assertEqual({s["id"] for s in selected.selected}, {"cctv-atcs", *VERIFIED, "cctv-kota"})
        unmapped = selected.summary()["unmapped_visible_entries"]
        self.assertEqual(len(unmapped), 8)
        self.assertNotIn("cctv.atcs-kota", {e["source"] for e in unmapped})

    def test_hidden_surveillance_parent_still_excludes_all_cctv(self):
        self.assertEqual(scope(active=False).selected, [])

    def test_registry_builds_verified_adapters_without_network(self):
        selected = scope()
        with patch("portal_scope.fetch_scope", return_value=selected), \
             patch("extractors.registry.CCTVNativeAdapter",
                   side_effect=lambda groups: CCTVNativeAdapter(groups, http=Mock())), \
             patch("extractors.registry.SupersetAdapter") as superset, \
             patch("extractors.registry.TableauLookerAdapter") as web:
            adapters = configured_adapters(ROOT / "ingestion_sources.json")
        self.assertIsInstance(adapters, ScopedAdapters)
        self.assertIs(adapters.portal_scope, selected)
        self.assertEqual(len(adapters), 11)
        superset.assert_not_called()
        web.assert_not_called()
        for adapter in adapters:
            target = adapter.discover()[0]
            self.assertEqual(parse_qs(urlparse(target.source_url).query)["filter[location]"],
                             [target.dashboard_id])
            adapter.http.get.assert_not_called()

    def test_every_new_group_keeps_pagination_identity_and_chunk_coverage(self):
        selected, records = scope(), {}
        for group in VERIFIED:
            with self.subTest(group=group):
                http = Mock()
                adapter = CCTVNativeAdapter([group], http=http)
                target = adapter.discover()[0]
                def page(start, end, nxt=None):
                    return Mock(json=lambda: {
                        "data": [{"id": i, "location": group, "name": "camera " + str(i)}
                                 for i in range(start, end)],
                        "meta": {"total": 123}, "links": {"next": nxt},
                    })
                http.get.side_effect = [page(0, 100, target.source_url + "&page=2"), page(100, 123)]
                with patch("contracts.utcnow", return_value=FIXED_TIME):
                    record = adapter.extract(target)[0]
                self.assertEqual(http.get.call_count, 2)
                self.assertEqual(record.metadata["pages_fetched"], 2)
                self.assertTrue(record.metadata["is_complete"])
                self.assertEqual(len({r["id"] for r in record.raw_rows}), 123)
                records[record.id] = record
        records, excluded = selected.apply(records)
        self.assertEqual(excluded, [])
        self.assertEqual(len(records), 9)
        docs = build_documents(records)
        self.assertEqual(docs, build_documents(records))
        self.assertTrue(validate_chunks(records, docs))
        self.assertTrue(all(d["metadata"]["portal_visible"] for d in docs))
        self.assertEqual({d["metadata"]["dashboard_id"] for d in docs}, set(VERIFIED))


if __name__ == "__main__":
    unittest.main()
