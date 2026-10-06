"""Source -> discover -> extract -> normalize/validate -> delta -> staging/chunks."""
from __future__ import annotations
import argparse
import logging
from pathlib import Path
import time
from contracts import utcnow
from delta_checker import DeltaChecker
from etl_common import ROOT, atomic_write_json, ingestion_lock, publish_bundle, recover_publication, setup_logging
from extractor import DEFAULT_MIN_SUCCESS_RATIO, ExtractionAborted, error_summary, extract_all
from transformers import build_documents, load_staging, staging_payload
from ingestion_config import ConfigurationError, startup_check

log = logging.getLogger("etl.main")

def run(force=False, include_known_bad=False,
        min_success_ratio=DEFAULT_MIN_SUCCESS_RATIO, *, adapters=None, output_dir=ROOT,
        config_path=ROOT / "ingestion_sources.json", max_chars=1500, now=None):
    """0=ok, 2=usable partial, 1=aborted. Known-good snapshots survive an abort."""
    if adapters is None:
        try:
            startup_check(config_path, output_dir)
        except ConfigurationError as exc:
            print(f"[config][error] {exc}", flush=True)
            return 1
        print("[ingestion] Starting extraction...", flush=True)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {"status": "aborted", "started_at": now or utcnow(), "failures": []}
    documents = None
    handler = logging.FileHandler(output_dir / "ingestion_errors.log", encoding="utf-8")
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    etl_log = logging.getLogger("etl")
    etl_log.addHandler(handler)
    try:
        with ingestion_lock(output_dir):
            try:
                recover_publication(output_dir)
                previous = load_staging(output_dir / "staging_idmc_data.json",
                                        output_dir / "staging_superset_data.json")
                if adapters is None:
                    from extractors.registry import configured_adapters
                    adapters = configured_adapters(config_path)
                result = extract_all(previous, adapters=adapters, min_success_ratio=min_success_ratio,
                                     include_known_bad=include_known_bad, now=now,
                                     record_validator=lambda records: build_documents(records, max_chars))
                report = result.report
                checker = DeltaChecker(output_dir / "delta_state_v2.json", config={"max_chunk_chars": max_chars, "row_order": "canonical_json"})
                delta = checker.check(result.records, force=force)
                # Cheap deterministic regeneration always refreshes metadata and repairs missing chunks.
                documents = build_documents(result.records, max_chars)
                report.update(chunks_produced=len(documents), content_changed=len(delta.content_changed_ids),
                              metadata_changed=len(delta.metadata_changed_ids),
                              regenerate=delta.regenerate, delta_reason=delta.reason)
                # Prepare and validate everything BEFORE replacing any known-good artifact.
                publish_bundle(output_dir, {
                    "staging_idmc_data.json": staging_payload(result.records),
                    "ready_for_vector_db_v2.json": documents,
                    "delta_state_v2.json": delta.state_payload(),  # commit last
                })
            except ExtractionAborted as exc:
                report = exc.report or report
                report.update(status="aborted", abort_reason=str(exc))
            except (Exception, SystemExit) as exc:
                category, message = error_summary(exc)
                log.error("Ingestion aborted [%s]: %s", category, message)
                report.update(status="aborted", abort_reason=message)
                report.setdefault("failures", []).append({"stage": "pipeline", "category": category, "message": message})
                categories = report.setdefault("failures_by_category", {})
                categories[category] = categories.get(category, 0) + 1
            report["duration_s"] = round(time.monotonic() - started, 3)
            for key in ("targets_discovered", "attempted", "succeeded", "failed", "carried_forward",
                        "skipped", "metadata_only", "records_produced", "chunks_produced"):
                report.setdefault(key, 0)
            atomic_write_json(output_dir / "extraction_report_v2.json", report)
    except Exception as exc:
        log.error("Cannot acquire lock/write report: %s", type(exc).__name__)
        return 1
    finally:
        etl_log.removeHandler(handler)
        handler.close()
    if report["status"] == "aborted":
        return 1
    return 2 if report["status"] == "partial" else 0

def main():
    parser = argparse.ArgumentParser(description="Reproducible IDMC ingestion and whole-row chunking")
    parser.add_argument("--config", type=Path, default=ROOT / "ingestion_sources.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--max-chunk-chars", type=int, default=1500)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--include-known-bad", action="store_true")
    parser.add_argument("--min-success-ratio", type=float, default=DEFAULT_MIN_SUCCESS_RATIO)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    return run(force=args.force, include_known_bad=args.include_known_bad,
               min_success_ratio=args.min_success_ratio, output_dir=args.output_dir, config_path=args.config,
               max_chars=args.max_chunk_chars)

if __name__ == "__main__":
    raise SystemExit(main())
