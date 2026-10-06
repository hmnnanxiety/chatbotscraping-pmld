"""Targeted Fiber Optic validation only; never runs the full source registry."""
import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from contracts import utcnow
from etl_common import ROOT, atomic_write_json, canonical_json
from extractors.registry import source_inventory
from extractors.tableau_public import TableauPublicAdapter
from portal_scope import fetch_scope
from transformers import build_documents, validate_chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime/tableau-targeted")
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if out in {ROOT.resolve(), (ROOT / "runtime").resolve()}:
        raise SystemExit("Use an isolated validation subdirectory, not primary runtime/root")
    config = json.loads((ROOT / "ingestion_sources.json").read_text(encoding="utf-8-sig"))
    sources = [s for s in config["tableau_sources"] if s["id"] == "tableau-jaringan-diy"]
    if len(sources) != 1: raise SystemExit("Expected exactly one configured Fiber Optic source")
    source = sources[0]
    print("Verifying current portal mapping...", flush=True)
    scope = fetch_scope(source_inventory({"superset": False,"tableau_sources": sources}))
    if len(scope.selected) != 1: raise SystemExit("Fiber Optic is not currently mapped/visible")
    names = ("staging_idmc_data.json","ready_for_vector_db_v2.json","delta_state_v2.json","extraction_report_v2.json")
    before = {n: hashlib.sha256((ROOT/"runtime"/n).read_bytes()).hexdigest() for n in names if (ROOT/"runtime"/n).exists()}
    checked, signatures, final = utcnow(), [], None
    for _ in range(2):
        print(f"Targeted Fiber Optic browser run {_+1}/2...", flush=True)
        adapter = TableauPublicAdapter(source)
        targets = adapter.discover()
        with patch("contracts.utcnow", return_value=checked): records = {r.id:r for r in adapter.extract(targets[0])}
        records, excluded = scope.apply(records)
        if excluded: raise SystemExit("Extracted record outside verified portal scope")
        docs = build_documents(records); validate_chunks(records,docs)
        final = {"records":[r.to_dict() for r in records.values()],"chunks":docs}
        signatures.append(hashlib.sha256(canonical_json(final).encode()).hexdigest())
    if signatures[0] != signatures[1]: raise SystemExit("Source changed/non-deterministic validation; no output published")
    after = {n: hashlib.sha256((ROOT/"runtime"/n).read_bytes()).hexdigest() for n in names if (ROOT/"runtime"/n).exists()}
    if before != after: raise SystemExit("Primary runtime unexpectedly changed")
    record = final["records"][0]
    report = {"status":"PARTIAL", "visible_metrics":"LIVE_OK", "source":source["url"],
              "portal_entries":scope.matches(scope.selected[0]),"rows_extracted":len(record["raw_rows"]),
              "fields":record["metadata"]["columns"],"values":record["raw_rows"],
              "records_produced":len(final["records"]),"chunks_produced":len(final["chunks"]),
              "source_update_date":record["metadata"]["source_update_date"],
              "unsupported_worksheets":[t.name for t in targets if t.options.get("skip_reason")],
              "detailed_data_status":record["metadata"]["detailed_data_status"],
              "byte_identical_normalized_runs":True,"sha256":signatures[0],
              "row_coverage":"validated; no lost/duplicated positions", "primary_runtime_unchanged":True}
    atomic_write_json(out / "validation_data.json",final)
    atomic_write_json(out / "validation_report.json",report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == "__main__": main()
