"""Offline scoped Looker reproduction; synthetic fixtures, no portal requests."""
import argparse
import hashlib
import json
from pathlib import Path

from extractors.looker_studio import LookerStudioAdapter
from extractors.registry import source_inventory
from main_orchestrator import run
from portal_scope import PortalScope, ScopedAdapters
from tests.helpers import FIXED_TIME
from tests.looker_fixtures import FixtureBrowser
from transformers import load_staging, validate_chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("runtime/frontend-demo"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "ingestion_sources.json").read_text(encoding="utf-8-sig"))
    items = [i for i in source_inventory(config) if i["source_type"] == "looker_studio"]
    menus = [{"id": n + 1, "name": "Synthetic portal " + i["name"], "status": 1,
              "source": i["config"]["url"]} for n, i in enumerate(items)]
    scope = PortalScope.from_payloads(items, menus, {"data": [{"dashboard_url": None}]})
    signatures = []
    for _ in range(2):
        adapters = ScopedAdapters([LookerStudioAdapter(i["config"], FixtureBrowser) for i in scope.selected], scope)
        if run(adapters=adapters, output_dir=args.output_dir, now=FIXED_TIME, max_chars=600):
            raise SystemExit("Frontend reproduction failed")
        signatures.append({n: hashlib.sha256((args.output_dir / n).read_bytes()).hexdigest() for n in
                           ("staging_idmc_data.json", "ready_for_vector_db_v2.json", "delta_state_v2.json")})
    if signatures[0] != signatures[1]:
        raise SystemExit("Non-deterministic frontend output")
    records = load_staging(args.output_dir / "staging_idmc_data.json")
    docs = json.loads((args.output_dir / "ready_for_vector_db_v2.json").read_text(encoding="utf-8"))
    validate_chunks(records, docs, 600)
    if not all(d["metadata"]["portal_visible"] for d in docs):
        raise SystemExit("Missing visible portal provenance")
    print(json.dumps({"identical_runs": True, "records": len(records),
                      "rows": sum(len(r.raw_rows) for r in records.values()), "chunks": len(docs),
                      "row_coverage": "validated; no lost/duplicated positions", "sha256": signatures[0]}, indent=2))


if __name__ == "__main__":
    main()
