"""Only the two configured public SMA dashboards; never full ingestion/publication."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from contracts import utcnow
from etl_common import ROOT, atomic_write_json, canonical_json
from extractor import classify_error, execute_adapter, discover_adapter
from extractors.grafana_public import GrafanaPublicAdapter
from extractors.registry import source_inventory
from portal_scope import fetch_scope
from transformers import build_documents, validate_chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime/grafana-targeted")
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if not out.is_relative_to((ROOT / "runtime").resolve()) or out == (ROOT / "runtime").resolve():
        raise SystemExit("Use an isolated subdirectory inside runtime")
    config = json.loads((ROOT / "ingestion_sources.json").read_text(encoding="utf-8-sig"))
    sources = config["grafana_sources"]
    if {s["id"] for s in sources} != {"grafana-sma-yogyakarta", "grafana-sma-mudik"} or len(sources) != 2:
        raise SystemExit("Expected exactly the two configured SMA reports")
    scope = fetch_scope(source_inventory({"superset": False, "grafana_sources": sources}))
    if len(scope.selected) != 2: raise SystemExit("Both SMA sources must currently be portal-visible")
    names = ("staging_idmc_data.json", "ready_for_vector_db_v2.json", "delta_state_v2.json", "extraction_report_v2.json")
    def hashes():
        return {n: hashlib.sha256((ROOT/"runtime"/n).read_bytes()).hexdigest() for n in names if (ROOT/"runtime"/n).exists()}
    before = hashes()
    checked, anchor = utcnow(), datetime.now(timezone.utc)
    report, records = [], {}
    for source in sources:
        print("Discovering " + source["name"], flush=True)
        a = GrafanaPublicAdapter(source, anchor=anchor)
        item = {"source_id": source["id"], "url": source["url"], "panels": [], "unsupported_panels": [], "issues": []}
        try:
            targets = discover_adapter(a)
        except Exception as exc:
            item.update(status="FAILED", issues=[{"stage": "discovery", "category": classify_error(exc), "error_type": type(exc).__name__}])
            report.append(item); continue
        item["non_data_panels"] = a.non_data_panels
        for target in targets:
            if target.options.get("skip_reason"):
                item["unsupported_panels"].append({"panel_id": target.chart_id, "name": target.name,
                                                    "reason": target.options["skip_reason"]})
                continue
            print("  Query " + target.chart_id + " " + target.name, flush=True)
            try:
                with patch("contracts.utcnow", return_value=checked): extracted = execute_adapter(a, target)
                selected, excluded = scope.apply({r.id: r for r in extracted})
                if excluded: raise ValueError("Result outside verified portal scope")
                docs = build_documents(selected); validate_chunks(selected, docs)
                records.update(selected)
                item["panels"].append({"panel_id": target.chart_id, "name": target.name,
                                       "type": target.options["panel"]["type"],
                                       "rows": sum(len(r.raw_rows) for r in selected.values()), "records": len(selected),
                                       "columns": sorted({column for r in selected.values() for column in r.metadata["columns"]}),
                                       "no_data": all(r.metadata["no_data"] for r in selected.values())})
            except Exception as exc:
                item["issues"].append({"panel_id": target.chart_id, "category": classify_error(exc), "error_type": type(exc).__name__})
        item.update(status="FAILED" if not item["panels"] else "PARTIAL" if item["issues"] or item["unsupported_panels"] else "LIVE_OK",
                    rows=sum(p["rows"] for p in item["panels"]), records=sum(p["records"] for p in item["panels"]))
        report.append(item)
    docs = build_documents(records); validate_chunks(records, docs)
    docs2 = build_documents(dict(reversed(list(records.items()))))
    byte_identical = canonical_json(docs) == canonical_json(docs2)
    if not byte_identical or before != hashes(): raise SystemExit("Determinism/runtime isolation check failed")
    summary = {"reports": report, "checked_at": checked, "rows": sum(len(r.raw_rows) for r in records.values()),
               "records": len(records), "chunks": len(docs), "deterministic_transform_replay": byte_identical,
               "lost_row_positions": 0, "duplicated_row_positions": 0, "primary_runtime_unchanged": True,
               "portal_scope": scope.summary(), "live_snapshot_repeated": False,
               "note": "Determinism checked on identical received input, not a changing live time window"}
    atomic_write_json(out / "validation_data.json", {"records": [r.to_dict() for _, r in sorted(records.items())], "chunks": docs})
    atomic_write_json(out / "validation_report.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if any(r["status"] == "FAILED" or r["issues"] for r in report): raise SystemExit(1)


if __name__ == "__main__": main()
