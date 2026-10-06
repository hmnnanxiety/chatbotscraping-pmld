"""Whole-row, deterministic chunking with bounded context and explicit oversize policy."""
import json
from collections import defaultdict
from contracts import NormalizedRecord
from etl_common import TRANSFORM_VERSION, canonical_json, md5_hex

DEFAULT_MAX_CHARS = 1500

def scalar_metadata(metadata):
    return {k: (v if isinstance(v, (str, bool, int, float)) else canonical_json(v))
            for k, v in metadata.items() if v is not None}

def _brief(value, limit=120):
    text = value if isinstance(value, str) else canonical_json(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."

def _header(record):
    m = record.metadata
    parts = [f"Source: {_brief(m['source_name'])}",
             f"Dashboard: {_brief(m['dashboard_name'])}",
             f"Chart: {_brief(m['chart_name'])}"]
    for label, key in (("Period", "statistical_period"), ("Unit", "unit"), ("Filters", "effective_filters")):
        if m.get(key) not in (None, [], {}):
            parts.append(f"{label}: {_brief(m[key])}")
    return "\n".join(parts)

def _text(header, start, end, total, lines):
    return f"{header}\nRows {start}-{end} of {total} (canonical order)\n\n" + "\n".join(lines)

def _record_chunks(record, max_chars):
    record = record.normalized()
    rows = record.raw_rows
    header = _header(record)
    if len(_text(header, len(rows), len(rows), len(rows), [])) >= max_chars:
        raise ValueError(f"Context alone exceeds chunk budget: {record.id}")
    groups, group, start = [], [], 1
    for number, row in enumerate(rows, 1):
        line = canonical_json(row)
        if group and len(_text(header, start, number, len(rows), group + [line])) > max_chars:
            groups.append((start, number - 1, group))
            group, start = [], number
        group.append(line)
    if group:
        groups.append((start, len(rows), group))
    if not rows:
        message = "Metadata only; no table extracted." if record.metadata["extraction_mode"] == "metadata_only" else "Snapshot contains zero rows."
        description = record.metadata.get("description")
        if description:
            remaining = max_chars - len(_text(header, 0, 0, 0, [message])) - 1
            if remaining > 10:
                message += "\n" + _brief(description, min(remaining, 240))
        groups = [(0, 0, [message])]
    docs = []
    for index, (first, last, lines) in enumerate(groups):
        text = _text(header, first, last, len(rows), lines)
        oversize = len(text) > max_chars
        if oversize and not (first == last and first > 0):
            raise ValueError("Only one indivisible row may exceed the chunk budget")
        metadata = scalar_metadata({
            **record.metadata, "record_id": record.id, "target_id": record.target_id,
            "source_type": record.source_type, "dashboard_id": record.dashboard_id,
            "chart_id": record.chart_id, "slice_id": record.chart_id,
            "slice_name": record.metadata["chart_name"], "result_index": record.result_index,
            "row_start": first, "row_end": last, "total_rows": len(rows),
            "chunk_index": index, "chunk_count": len(groups), "oversized_row": oversize,
            "partial_row": False, "max_chunk_chars": max_chars, "row_order": "canonical_json",
            "content_hash": md5_hex(text), "transform_version": TRANSFORM_VERSION,
            "extracted_at": record.metadata["last_successful_fetch_at"],
        })
        docs.append({"id": f"v2-{md5_hex(record.id)}-chunk{index}", "page_content": text, "metadata": metadata})
    return docs

def build_documents(records, max_chars=DEFAULT_MAX_CHARS):
    if type(max_chars) is not int or max_chars < 256:
        raise ValueError("max_chars must be an integer >= 256")
    docs = []
    for key, record in sorted(records.items()):
        if key != record.id:
            raise ValueError("Record map key does not match record_id")
        docs.extend(_record_chunks(record, max_chars))
    validate_chunks(records, docs, max_chars)
    return docs

def validate_chunks(records, docs, max_chars=DEFAULT_MAX_CHARS):
    """Check identity, provenance, serialized rows, full range coverage and budget."""
    groups, ids = defaultdict(list), set()
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("page_content"), str) or not doc["page_content"].strip():
            raise ValueError("Empty/invalid chunk content")
        if not isinstance(doc.get("id"), str) or not doc["id"] or doc["id"] in ids:
            raise ValueError("Missing or duplicate chunk IDs")
        ids.add(doc["id"])
        meta = doc.get("metadata")
        if not isinstance(meta, dict) or not meta.get("record_id") or not meta.get("target_id"):
            raise ValueError("Missing chunk identity/provenance")
        if meta["record_id"] not in records:
            raise ValueError("Chunk references unknown record")
        if meta.get("content_hash") != md5_hex(doc["page_content"]):
            raise ValueError("Chunk content hash mismatch")
        for name in ("row_start", "row_end", "total_rows", "chunk_index", "chunk_count"):
            if type(meta.get(name)) is not int:
                raise ValueError(f"Invalid chunk {name}")
        groups[meta["record_id"]].append(doc)
    if set(groups) != set(records):
        raise ValueError("Missing chunks for normalized records")
    for key, record in records.items():
        record = record.normalized()
        group = sorted(groups[key], key=lambda d: d["metadata"]["chunk_index"])
        recovered, next_row = [], 1
        for index, doc in enumerate(group):
            m, text = doc["metadata"], doc["page_content"]
            if doc["id"] != f"v2-{md5_hex(key)}-chunk{index}" or m["chunk_index"] != index or m["chunk_count"] != len(group):
                raise ValueError("Invalid chunk identity/index/count")
            expected_provenance = scalar_metadata(record.metadata)
            for field, value in expected_provenance.items():
                if m.get(field) != value:
                    raise ValueError(f"Chunk provenance mismatch: {field}")
            if m["target_id"] != record.target_id or m.get("source_type") != record.source_type or m["total_rows"] != len(record.raw_rows):
                raise ValueError("Chunk target/source/total mismatch")
            if not text.startswith(_text(_header(record), m["row_start"], m["row_end"], len(record.raw_rows), [])):
                raise ValueError("Chunk lost source/chart context")
            if m.get("partial_row") is not False or m.get("transform_version") != TRANSFORM_VERSION:
                raise ValueError("Invalid partial-row/version marker")
            if record.raw_rows:
                if m["row_start"] != next_row or not m["row_start"] <= m["row_end"] <= len(record.raw_rows):
                    raise ValueError("Invalid/overlapping row ranges")
                part = [json.loads(line) for line in text.split("\n\n", 1)[1].splitlines()]
                if len(part) != m["row_end"] - m["row_start"] + 1:
                    raise ValueError("Chunk row count/range mismatch")
                recovered.extend(part)
                next_row = m["row_end"] + 1
            elif len(group) != 1 or m["row_start"] != 0 or m["row_end"] != 0:
                raise ValueError("Empty snapshot must have one 0-0 chunk")
            oversize = len(text) > max_chars
            if type(m.get("oversized_row")) is not bool or m["oversized_row"] != oversize:
                raise ValueError("Incorrect oversize marker")
            if oversize and not (m["row_start"] == m["row_end"] and m["row_start"] > 0):
                raise ValueError("Multi-row chunk exceeds budget")
        if canonical_json(recovered) != canonical_json(record.raw_rows):
            raise ValueError("Lost, duplicated or reordered rows in chunks")
    return True
