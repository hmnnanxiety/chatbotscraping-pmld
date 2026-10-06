"""Offline clean-checkout acceptance test; writes synthetic outputs, no services."""
import argparse
import hashlib
from pathlib import Path
from main_orchestrator import run
from transformers import load_staging, validate_chunks
from tests.helpers import FakeAdapter, FIXED_TIME, record
import json

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".ingestion-demo"))
    args = parser.parse_args()
    adapter = FakeAdapter([record("small"), record("large", rows=[{"id": i, "value": "district " + str(i) * 30} for i in range(150)])])
    hashes = []
    for _ in range(2):
        if run(adapters=[adapter], output_dir=args.output_dir, now=FIXED_TIME, max_chars=600):
            raise SystemExit("Reproduction failed")
        hashes.append({name: hashlib.sha256((args.output_dir / name).read_bytes()).hexdigest() for name in
                       ("staging_idmc_data.json", "ready_for_vector_db_v2.json", "delta_state_v2.json")})
    assert hashes[0] == hashes[1], "Non-deterministic outputs"
    records = load_staging(args.output_dir / "staging_idmc_data.json")
    docs = json.loads((args.output_dir / "ready_for_vector_db_v2.json").read_text(encoding="utf-8"))
    validate_chunks(records, docs, 600)
    print(json.dumps({"identical_runs": True, "records": len(records), "rows": sum(len(r.raw_rows) for r in records.values()),
                      "chunks": len(docs), "sha256": hashes[0]}, indent=2))

if __name__ == "__main__":
    main()
