import copy
import json
import unittest
from contracts import NormalizedRecord
from etl_common import canonical_json, md5_hex
from transformers import build_documents, validate_chunks
from tests.helpers import record

class ContractTests(unittest.TestCase):
    def test_normalization_stable_and_copies_input(self):
        r = record(rows=[{"b": 2, "a": 1}, {"a": 0}])
        a = r.normalized()
        r.raw_rows.reverse()
        self.assertEqual(canonical_json(a.to_dict()), canonical_json(r.normalized().to_dict()))

    def test_missing_identity_rejected(self):
        data = record().to_dict()
        del data["record_id"]
        with self.assertRaises(ValueError):
            NormalizedRecord.from_dict(data)
        r = record()
        r.target_id = ""
        with self.assertRaises(ValueError):
            r.validate()

    def test_required_provenance(self):
        for key in ("source_name", "chart_name", "source_url", "statistical_period", "last_successful_fetch_at"):
            r = record()
            del r.metadata[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                r.validate()

    def test_invalid_shapes_timestamps_and_numbers(self):
        for value in ("not a date", "2026-09-29T12:00:00"):
            r = record()
            r.metadata["last_checked_at"] = value
            with self.assertRaises(ValueError):
                r.validate()
        r = record()
        r.metadata["last_successful_fetch_at"] = "2027-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            r.validate()
        for rows in ({}, [[1]], [{"x": float("nan")}], [{"x": float("inf")}]):
            r = record()
            r.raw_rows = rows
            with self.assertRaises((TypeError, ValueError)):
                r.validate()

class ChunkTests(unittest.TestCase):
    def test_small_chart_one_chunk_without_internal_metadata(self):
        r = record()
        r.metadata["metrics"] = [{"internal": "x" * 9000}]
        docs = build_documents({r.id: r})
        self.assertEqual(len(docs), 1)
        self.assertNotIn("internal", docs[0]["page_content"])
        self.assertIn("internal", docs[0]["metadata"]["metrics"])

    def test_deterministic_and_lossless_multichunk(self):
        r = record(rows=[{"id": i, "value": "district " + str(i) * 30} for i in range(150)])
        records = {r.id: r}
        first = build_documents(records, 600)
        r.raw_rows.reverse()
        second = build_documents(records, 600)
        self.assertEqual(first, second)
        self.assertGreater(len(first), 1)
        self.assertTrue(validate_chunks(records, first, 600))
        recovered = []
        for d in first:
            self.assertLessEqual(len(d["page_content"]), 600)
            self.assertIn("Example source", d["page_content"])
            self.assertIn("Example dashboard", d["page_content"])
            self.assertIn("Chart 1", d["page_content"])
            recovered.extend(json.loads(line) for line in d["page_content"].split("\n\n", 1)[1].splitlines())
        self.assertEqual(recovered, r.normalized().raw_rows)
        self.assertEqual(len({row["id"] for row in recovered}), 150)

    def test_long_individual_row_is_whole_and_flagged(self):
        r = record(rows=[{"description": "x" * 5000, "v": 1.2345678912345}, {"description": "small", "v": None}])
        docs = build_documents({r.id: r}, 500)
        large = [d for d in docs if d["metadata"]["oversized_row"]]
        self.assertEqual(len(large), 1)
        m = large[0]["metadata"]
        self.assertEqual(m["row_start"], m["row_end"])
        self.assertFalse(m["partial_row"])
        self.assertIn("1.2345678912345", large[0]["page_content"])
        self.assertTrue(validate_chunks({r.id: r}, docs, 500))

    def test_duplicate_source_rows_and_empty_objects_preserved(self):
        r = record(rows=[{}, {"v": None}, {"v": 1}, {"v": 1}])
        docs = build_documents({r.id: r})
        self.assertEqual(docs[0]["metadata"]["total_rows"], 4)
        self.assertTrue(validate_chunks({r.id: r}, docs))

    def test_empty_snapshot_and_metadata_only_have_valid_zero_ranges(self):
        for mode in ("api", "metadata_only"):
            r = record(rows=[])
            r.metadata["extraction_mode"] = mode
            docs = build_documents({r.id: r})
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["metadata"]["row_start"], 0)
            self.assertTrue(validate_chunks({r.id: r}, docs))

    def test_corrupt_duplicate_missing_rows_ranges_provenance_rejected(self):
        r = record(rows=[{"id": i, "text": "x" * 80} for i in range(12)])
        original = build_documents({r.id: r}, 500)
        changes = [
            lambda docs: docs.append(copy.deepcopy(docs[0])),
            lambda docs: docs.pop(),
            lambda docs: docs[0]["metadata"].update(row_end=999),
            lambda docs: docs[0]["metadata"].pop("source_url"),
            lambda docs: docs[0].update(page_content=""),
        ]
        for mutate in changes:
            docs = copy.deepcopy(original)
            mutate(docs)
            with self.assertRaises(ValueError):
                validate_chunks({r.id: r}, docs, 500)
        docs = copy.deepcopy(original)
        header, body = docs[0]["page_content"].split("\n\n", 1)
        lines = body.splitlines()
        lines[0] = '{"id":999,"text":"changed"}'
        docs[0]["page_content"] = header + "\n\n" + "\n".join(lines)
        docs[0]["metadata"]["content_hash"] = md5_hex(docs[0]["page_content"])
        with self.assertRaisesRegex(ValueError, "Lost, duplicated"):
            validate_chunks({r.id: r}, docs, 500)
