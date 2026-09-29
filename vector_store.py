"""
vector_store.py

Thin adapter between the app and the (hypothetical) vector DB. The rest of
the codebase only depends on two functions, so swapping Chroma for Qdrant,
pgvector, etc. means rewriting just this file:

    search(query, top_k, dashboard_name) -> [{"content", "metadata", "score"}]
    index_documents(docs)                -> loads ready_for_vector_db.json

The implementation below is a reference using ChromaDB (local, persistent)
and Gemini embeddings:
    pip install chromadb google-genai python-dotenv

Load / refresh the index (also available as `main_orchestrator.py --load-vector-db`):
    python vector_store.py
"""

from __future__ import annotations

import functools
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import chromadb
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from etl_common import READY_PATH, read_json, setup_logging

load_dotenv()
log = logging.getLogger("vector_store")

CHROMA_PATH = os.environ.get("CHROMA_PATH", "./chroma_db")
# Current recommended model per Google's deprecations page (checked Sept 2026).
# gemini-embedding-001 also still works; text-embedding-004 was shut down in Jan 2026.
# Vectors from different models are incompatible, so the collection name includes
# the model: switching EMBED_MODEL starts a fresh collection instead of mixing spaces.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-2")
_IS_V2 = "embedding-2" in EMBED_MODEL
COLLECTION_NAME = os.environ.get("VECTOR_COLLECTION", f"dwh_knowledge_{EMBED_MODEL}")
EMBED_BATCH = 50
# Free tier allows 100 embedding requests/min per model, and each text in a batch
# counts as one. Throttle below that; set EMBED_RPM=0 on a paid tier to disable.
EMBED_RPM = int(os.environ.get("EMBED_RPM", "90"))
MAX_429_RETRIES = 6
# Drop hits below this cosine similarity. 0 = keep everything; tune against real queries.
MIN_SCORE = float(os.environ.get("VECTOR_MIN_SCORE", "0.0"))


@functools.lru_cache(maxsize=1)
def _genai_client() -> genai.Client:
    return genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


@functools.lru_cache(maxsize=1)
def _collection():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    return client.get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


def _embed_call(batch: List[str], kind: str) -> List[List[float]]:
    """One embed_content request, with 429 retry and pacing under EMBED_RPM."""
    if _IS_V2:
        fmt = (lambda t: f"task: search result | query: {t}") if kind == "query" \
            else (lambda t: f"title: none | text: {t}")
        contents: Any = [types.Content(parts=[types.Part.from_text(text=fmt(t))]) for t in batch]
        config = None
    else:
        contents = batch
        config = types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
        )

    for attempt in range(1, MAX_429_RETRIES + 1):
        started = time.monotonic()
        try:
            resp = _genai_client().models.embed_content(
                model=EMBED_MODEL, contents=contents, config=config
            )
            break
        except errors.ClientError as e:
            if getattr(e, "code", None) != 429 or attempt == MAX_429_RETRIES:
                raise
            match = re.search(r"retry in ([\d.]+)s", str(e))
            delay = float(match.group(1)) + 1 if match else 20.0 * attempt
            log.warning("Rate limited (429); sleeping %.0fs (attempt %d/%d)", delay, attempt, MAX_429_RETRIES)
            time.sleep(delay)

    if EMBED_RPM > 0:
        wait = len(batch) * 60 / EMBED_RPM - (time.monotonic() - started)
        if wait > 0:
            time.sleep(wait)
    return [e.values for e in resp.embeddings]


def _embed(texts: List[str], kind: str) -> List[List[float]]:
    """
    kind: "document" or "query".

    gemini-embedding-2 has no task_type parameter; the task goes into the text
    itself, and each input must be its own Content object (a plain list of
    strings would be aggregated into ONE embedding).
    gemini-embedding-001 uses task_type and embeds a list of strings separately.
    """
    vectors: List[List[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(_embed_call(texts[i : i + EMBED_BATCH], kind))
    if len(vectors) != len(texts):
        raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(vectors)}.")
    return vectors


# --------------------------------------------------------------------------- #
# Query side (used by app.py)
# --------------------------------------------------------------------------- #

def search(
    query: str, top_k: int = 5, dashboard_name: Optional[str] = None
) -> List[Dict[str, Any]]:
    vector = _embed([query], "query")[0]
    res = _collection().query(
        query_embeddings=[vector],
        n_results=top_k,
        where={"dashboard_name": dashboard_name} if dashboard_name else None,
        include=["documents", "metadatas", "distances"],
    )
    hits: List[Dict[str, Any]] = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        score = 1.0 - dist  # cosine distance -> similarity
        if score >= MIN_SCORE:
            hits.append({"content": doc, "metadata": meta, "score": score})
    return hits


# --------------------------------------------------------------------------- #
# Load side (used by the ETL)
# --------------------------------------------------------------------------- #

def index_documents(docs: List[Dict[str, Any]]) -> None:
    """
    Sync the collection to `docs`:
      - delete ids that no longer exist (charts removed / chart shrank)
      - re-embed only docs whose text changed (content_hash differs)
      - refresh metadata (extracted_at, row ranges) on the rest without re-embedding
    """
    col = _collection()
    new_ids = {d["id"] for d in docs}

    existing = col.get(include=["metadatas"])
    existing_hash = {
        doc_id: (meta or {}).get("content_hash")
        for doc_id, meta in zip(existing["ids"], existing["metadatas"])
    }

    stale_ids = [i for i in existing_hash if i not in new_ids]
    if stale_ids:
        col.delete(ids=stale_ids)

    changed = [d for d in docs if existing_hash.get(d["id"]) != d["metadata"]["content_hash"]]
    unchanged = [d for d in docs if existing_hash.get(d["id"]) == d["metadata"]["content_hash"]]

    if unchanged:
        col.update(ids=[d["id"] for d in unchanged], metadatas=[d["metadata"] for d in unchanged])

    # Embed + upsert one batch at a time so progress is saved as we go. If a
    # run dies midway (quota, network), the re-run skips everything already
    # stored, because those docs now match on content_hash.
    for i in range(0, len(changed), EMBED_BATCH):
        batch = changed[i : i + EMBED_BATCH]
        vectors = _embed([d["page_content"] for d in batch], "document")
        col.upsert(
            ids=[d["id"] for d in batch],
            documents=[d["page_content"] for d in batch],
            metadatas=[d["metadata"] for d in batch],
            embeddings=vectors,
        )
        log.info("Embedded %d/%d changed chunk(s)", min(i + EMBED_BATCH, len(changed)), len(changed))

    log.info(
        "Vector index synced: %d embedded, %d metadata-only, %d deleted (total %d)",
        len(changed), len(unchanged), len(stale_ids), col.count(),
    )


if __name__ == "__main__":
    setup_logging()
    documents = read_json(READY_PATH)
    if not documents:
        raise SystemExit(f"{READY_PATH} is missing or empty — run main_orchestrator.py first.")
    index_documents(documents)