"""Standalone v2 rechunking. No network requests and no timestamp refresh."""
import argparse
from pathlib import Path
from etl_common import READY_PATH, STAGING_PATH, atomic_write_json, ingestion_lock, recover_publication
from transformers import build_documents, load_staging

MAX_CHUNK_CHARS = 1500

def transform(staging_path=STAGING_PATH, out_path=READY_PATH, max_chars=MAX_CHUNK_CHARS):
    out_path = Path(out_path)
    with ingestion_lock(out_path.parent):
        recover_publication(out_path.parent)
        records = load_staging(staging_path)
        if not records:
            raise ValueError("No normalized records in staging")
        docs = build_documents(records, max_chars)
        atomic_write_json(out_path, docs)
    return docs

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, default=STAGING_PATH)
    parser.add_argument("--output", type=Path, default=READY_PATH)
    parser.add_argument("--max-chunk-chars", type=int, default=MAX_CHUNK_CHARS)
    args = parser.parse_args()
    docs = transform(args.staging, args.output, args.max_chunk_chars)
    print(f"Validated {len(docs)} chunks -> {args.output}")

if __name__ == "__main__":
    main()
