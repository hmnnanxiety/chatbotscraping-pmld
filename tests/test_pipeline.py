import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contracts import NORMALIZATION_VERSION
from delta_checker import DeltaChecker
from etl_common import atomic_write_json, publish_bundle, recover_publication
from extractor import classify_error, extract_all, ExtractionAborted
from main_orchestrator import run
from transformers import build_documents, load_staging, staging_payload
from tests.helpers import record, FakeAdapter, FIXED_TIME, LATER, http_error

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def run_etl(self, adapters, **kwargs):
        return run(adapters=adapters, output_dir=self.path, now=kwargs.pop("now", FIXED_TIME), **kwargs)
    def state_bytes(self):
        return {name: (self.path / name).read_bytes() for name in
                ("staging_idmc_data.json", "ready_for_vector_db_v2.json", "delta_state_v2.json")}
    def report(self):
        return json.loads((self.path / "extraction_report_v2.json").read_text())

    def test_success_and_identical_runs_byte_for_byte(self):
        adapter = FakeAdapter([record()])
        self.assertEqual(self.run_etl([adapter]), 0)
        first = self.state_bytes()
        self.assertEqual(self.run_etl([adapter]), 0)
        self.assertEqual(first, self.state_bytes())
        self.assertEqual(self.report()["delta_reason"], "unchanged")

    def test_one_failure_carries_old_rows_and_never_refreshes_success_time(self):
        one, two = record("1"), record("2")
        self.run_etl([FakeAdapter([one, two])])
        self.assertEqual(self.run_etl([FakeAdapter([one, two], {"1": http_error(404)})], now=LATER), 2)
        old = load_staging(self.path / "staging_idmc_data.json")[one.id]
        self.assertEqual(old.raw_rows, one.raw_rows)
        self.assertTrue(old.metadata["stale"])
        self.assertEqual(old.metadata["last_successful_fetch_at"], FIXED_TIME)
        self.assertEqual(old.metadata["last_checked_at"], LATER)
        self.assertEqual(self.report()["carried_forward"], 1)
        self.assertEqual(self.report()["failures_by_category"], {"http_404": 1})

    def test_first_run_failure_does_not_kill_healthy_target(self):
        self.assertEqual(self.run_etl([FakeAdapter([record("1"), record("2")], {"1": http_error(404)})]), 2)
        self.assertEqual(len(load_staging(self.path / "staging_idmc_data.json")), 1)

    def test_systemic_abort_preserves_all_three_good_files(self):
        self.run_etl([FakeAdapter([record()])])
        before = self.state_bytes()
        broken = [FakeAdapter([record(dashboard=str(i))], discovery_error=http_error(403)) for i in range(3)]
        self.assertEqual(self.run_etl(broken, now=LATER), 1)
        self.assertEqual(before, self.state_bytes())
        self.assertEqual(self.report()["status"], "aborted")
        self.assertIn("systemic", self.report()["abort_reason"])

    def test_minimum_success_threshold_protects_previous_state(self):
        self.run_etl([FakeAdapter([record()])])
        before = self.state_bytes()
        adapter = FakeAdapter([record(str(i)) for i in range(4)], {"1": http_error(404), "2": http_error(404), "3": http_error(404)})
        self.assertEqual(self.run_etl([adapter]), 1)
        self.assertEqual(before, self.state_bytes())

    def test_discovery_failure_is_counted_in_success_threshold(self):
        self.run_etl([FakeAdapter([record()])])
        before = self.state_bytes()
        broken = [FakeAdapter([record(dashboard=str(i))], discovery_error=ValueError("bad")) for i in range(3)]
        self.assertEqual(self.run_etl(broken + [FakeAdapter([record()])]), 1)
        self.assertEqual(before, self.state_bytes())

    def test_missing_target_is_carried_stale_not_deleted(self):
        one, two = record("1"), record("2")
        self.run_etl([FakeAdapter([one, two])])
        self.assertEqual(self.run_etl([FakeAdapter([one])], now=LATER), 2)
        records = load_staging(self.path / "staging_idmc_data.json")
        self.assertTrue(records[two.id].metadata["stale"])
        self.assertEqual(records[two.id].metadata["last_successful_fetch_at"], FIXED_TIME)

    def test_known_bad_skip_does_not_attempt_and_can_be_overridden(self):
        adapter = FakeAdapter([record()])
        target = adapter.discover()[0]
        target.options["skip_reason"] = "known timeout"
        adapter.discover = Mock(return_value=[target])
        adapter.extract = Mock(wraps=adapter.extract)
        self.run_etl([FakeAdapter([record()])])
        self.assertEqual(self.run_etl([adapter], now=LATER), 2)
        adapter.extract.assert_not_called()
        self.assertEqual(self.report()["skipped"], 1)
        self.assertEqual(self.run_etl([adapter], include_known_bad=True), 0)
        adapter.extract.assert_called_once()

    def test_metadata_only_degradation_carries_structured_data(self):
        one, two = record("1"), record("2")
        self.run_etl([FakeAdapter([one, two])])
        degraded = record("1", rows=[])
        degraded.metadata["extraction_mode"] = "metadata_only"
        self.assertEqual(self.run_etl([FakeAdapter([degraded, two])], now=LATER), 2)
        restored = load_staging(self.path / "staging_idmc_data.json")[one.id]
        self.assertEqual(restored.raw_rows, one.raw_rows)
        self.assertTrue(restored.metadata["stale"])

    def test_transform_failure_does_not_commit_or_overwrite(self):
        self.run_etl([FakeAdapter([record()])])
        before = self.state_bytes()
        with patch("main_orchestrator.build_documents", side_effect=ValueError("bad transform")):
            self.assertEqual(self.run_etl([FakeAdapter([record("2")])]), 1)
        self.assertEqual(before, self.state_bytes())

    def test_publication_replace_failure_rolls_back_all_artifacts(self):
        self.run_etl([FakeAdapter([record()])])
        before = self.state_bytes()
        import os
        replace = os.replace
        def fail_ready(source, target):
            if str(source).endswith("ready_for_vector_db_v2.json.new"):
                raise OSError("simulated disk failure")
            return replace(source, target)
        with patch("etl_common.os.replace", side_effect=fail_ready):
            self.assertEqual(self.run_etl([FakeAdapter([record(rows=[{"count": 99}])])]), 1)
        self.assertEqual(before, self.state_bytes())
        self.assertFalse((self.path / ".etl-transaction.json").exists())

    def test_interrupted_publication_recovered_on_startup(self):
        backup = self.path / ".etl-transaction-test"
        backup.mkdir()
        atomic_write_json(self.path / "staging_idmc_data.json", {"old": False})
        atomic_write_json(backup / "staging_idmc_data.json", {"old": True})
        atomic_write_json(self.path / ".etl-transaction.json", {
            "backup": backup.name, "files": [{"name": "staging_idmc_data.json", "existed": True}]})
        recover_publication(self.path)
        self.assertEqual(json.loads((self.path / "staging_idmc_data.json").read_text()), {"old": True})

    def test_invalid_staging_is_not_silently_discarded(self):
        path = self.path / "staging_idmc_data.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.run_etl([FakeAdapter([record()])]), 1)
        self.assertEqual(path.read_text(), "{broken")

    def test_missing_chunks_are_regenerated_even_when_delta_unchanged(self):
        self.run_etl([FakeAdapter([record()])])
        path = self.path / "ready_for_vector_db_v2.json"
        before = path.read_bytes()
        path.unlink()
        self.assertEqual(self.run_etl([FakeAdapter([record()])]), 0)
        self.assertEqual(before, path.read_bytes())

    def test_final_generation_failure_keeps_delta_uncommitted(self):
        self.run_etl([FakeAdapter([record()])])
        before = self.state_bytes()
        calls = 0
        def generate(records, budget):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("final generation failed")
            return build_documents(records, budget)
        with patch("main_orchestrator.build_documents", side_effect=generate):
            self.assertEqual(self.run_etl([FakeAdapter([record(rows=[{"count": 999}])])]), 1)
        self.assertEqual(before, self.state_bytes())

    def test_experimental_v2_snapshot_migration_has_explicit_record_id(self):
        r = record()
        raw = r.to_dict()
        del raw["record_id"]
        del raw["metadata"]["source_name"]
        del raw["metadata"]["chart_name"]
        path = self.path / "experimental.json"
        atomic_write_json(path, {"schema_version": 2, "records": {r.id: raw}})
        before = path.read_bytes()
        restored = load_staging(path)
        self.assertEqual(restored[r.id].record_id, r.id)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(staging_payload(restored)["normalization_version"], NORMALIZATION_VERSION)

class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "delta.json"
        self.r = record()
        self.checker = DeltaChecker(self.path, config={"max_chars": 1500})
        self.checker.commit(self.checker.check({self.r.id: self.r}))
    def tearDown(self):
        self.tmp.cleanup()
    def delta(self):
        return self.checker.check({self.r.id: self.r})

    def test_same_input_unchanged(self):
        self.assertFalse(self.delta().changed)

    def test_value_schema_and_name_changes_detected(self):
        original = copy.deepcopy(self.r)
        for mutate in (lambda r: r.raw_rows[0].update(count=43),
                       lambda r: r.raw_rows[0].update(new_column=None),
                       lambda r: r.metadata.update(chart_name="Renamed"),
                       lambda r: r.metadata.update(unit="rupiah")):
            self.r = copy.deepcopy(original)
            mutate(self.r)
            self.assertTrue(self.delta().regenerate)

    def test_timestamp_only_change_not_content(self):
        before = build_documents({self.r.id: self.r})[0]["page_content"]
        self.r.metadata.update(last_checked_at=LATER, last_successful_fetch_at=LATER,
                               source_update_date="30 September 2026", source_update_note="Updated today")
        delta = self.delta()
        self.assertEqual(delta.content_changed_ids, [])
        self.assertEqual(delta.metadata_changed_ids, [self.r.id])
        self.assertFalse(delta.regenerate)
        self.assertEqual(before, build_documents({self.r.id: self.r})[0]["page_content"])

    def test_version_change_forces_regeneration(self):
        checker = DeltaChecker(self.path, transform_version="2.2.0", config={"max_chars": 1500})
        self.assertEqual(checker.check({self.r.id: self.r}).reason, "version_changed")

    def test_config_change_forces_regeneration(self):
        checker = DeltaChecker(self.path, config={"max_chars": 500})
        self.assertEqual(checker.check({self.r.id: self.r}).reason, "config_changed")
