# Standalone ingestion contract, revision 2.1.0

## Scope and adapters

Source → discovery → extraction → normalization → validation → delta →
staging → chunking → JSON output.

The flat module layout is intentionally retained. All local imports resolve inside
this project. A new source implements `source_type`, `discover()` returning
SourceTarget objects, and `extract(target)` returning NormalizedRecord objects.
Optional `adapter_id`/`target_prefixes` identify ownership for stale carry-forward.

Superset handles session cookies, per-dashboard guest tokens with refresh,
bootstrap numeric IDs, stored query_context, effective default filters, scope and
exclusions. Unknown filter forms fail explicitly. Multi-query results have distinct
result_index values. Native CCTV follows links.next, checks origin/filter,
deduplicates camera IDs and verifies totals; pagination loops or inconsistent
snapshots fail. Tableau/Looker extracts public metadata or explicitly configured
same-origin CSV/JSON exports; unavailable tables remain labelled metadata_only.

## Normalized record

`schema_version = 2`; `normalization_version = "2.1.0"`;
`transform_version = "2.1.0"`.

```json
{
  "record_id": "source:dashboard:chart:result0",
  "target_id": "source:dashboard:chart",
  "source_type": "source",
  "dashboard_id": "dashboard",
  "chart_id": "chart",
  "result_index": 0,
  "raw_rows": [{"count": 42}],
  "metadata": {
    "source_name": "Example source",
    "dashboard_name": "Example dashboard",
    "chart_name": "Example chart",
    "source_url": "https://example.org/data",
    "statistical_period": "2026",
    "effective_filters": [],
    "source_update_date": null,
    "unit": "people",
    "last_checked_at": "2026-09-29T12:00:00+00:00",
    "last_successful_fetch_at": "2026-09-29T12:00:00+00:00",
    "stale": false,
    "is_complete": "unknown",
    "extraction_mode": "api"
  }
}
```

Unknown period/unit/publication date remain null. Source publication dates can
retain their original textual format. Observation timestamps must be timezone-aware
ISO-8601; unknown timestamps are allowed only on stale legacy snapshots.
Validation rejects missing identities/provenance, invalid shapes, non-JSON values,
NaN/infinity, timestamp inconsistencies and numeric rows in metadata-only records.

## Reliability and publication

Retry/backoff covers transient transport/status failures, not permanent 404/422 or
certificate errors. Exceptions are classified per target; healthy targets continue.
Previously successful rows survive failures as stale; successful-fetch time is never
updated by carry-forward. Missing/disconnected targets are retained with stale status.

Known-bad Superset charts are skipped by default; use `--include-known-bad` to retry.
The default success threshold is 50%, including unavailable targets from failed
discovery. Three consecutive systemic-failure scopes abort. A wholly failed extraction
never replaces successful prior artifacts.

All normalized records and chunks validate before publication. Output serialization
is deterministic. Prepared files are atomically replaced with delta state last.
Failures trigger rollback; an interrupted-publication journal recovers on next run.
Kernel locking prevents two writers in one output directory. External consumers should
read after job completion or coordinate via the same lock; this is not a cross-file
database transaction for unlocked concurrent readers.

Run reports include discovered/attempted/succeeded/failed/skipped/carried-forward,
metadata-only, unavailable targets, record/chunk totals, failure categories, duration,
and ok/partial/aborted status. An aborted run can update its report without replacing
known-good staging/chunks/delta.

## Determinism and chunk integrity

Normalization canonicalizes object keys and sorts rows by canonical JSON. The same
input (including observation metadata) and configuration generate byte-identical
staging and chunk content. Timestamps legitimately differ between live observations,
but do not create content changes. Semantically meaningful names, schema, rows,
filters, periods, units and provenance are part of content hashes. Technical/freshness
metadata has a separate hash. Transform version or chunk configuration changes force
regeneration. Rechunking never refreshes source timestamps.

Chunk IDs derive from record identity and chunk index. Rows remain intact, precise
and in canonical order. Input duplicates are preserved, not amplified or removed.
Each chunk repeats readable source/dashboard/chart and brief period/unit/filter
context; verbose technical information stays attached as metadata.

Budget includes all readable text. Only a single indivisible row may exceed it,
with oversized_row=true and partial_row=false. Small datasets remain one chunk when
they fit. Empty datasets/metadata-only records produce one chunk with range 0-0.
Validator reconstructs serialized rows and checks full content/order/multiplicity,
row_start/end/total_rows, provenance, IDs, hashes, budget and context.

Experimental v2 and v1 staging can be explicitly loaded/migrated without mutating
their source file. No old snapshots are bundled or needed for tests.

## Operation

```powershell
python -B main_orchestrator.py --output-dir runtime --max-chunk-chars 1500
python -B main_orchestrator.py --output-dir runtime --min-success-ratio 0.5 --include-known-bad
python -B transformer.py --staging runtime/staging_idmc_data.json --output runtime/ready_for_vector_db_v2.json
python -B -m unittest discover -s tests -v
python -B -m tests.reproduce --output-dir .ingestion-demo
```

The output filename ready_for_vector_db_v2.json is retained only for compatibility:
its content is ordinary JSON and no downstream service is invoked.

Requirements pin requests, python-dotenv, tenacity and HTTP transitive dependencies.
TLS verification is enabled. Prefer IDMC_CA_BUNDLE for a trusted proxy/private CA.
IDMC_VERIFY_TLS=false is a development-only workaround with one application warning.
Only InsecureRequestWarning is suppressed in that mode; other warnings remain.
IDMC_CA_BUNDLE takes precedence. Local .env loads once before client creation;
process environment takes precedence. Restart after configuration changes.
Use `python -m tests.smoke_config` for an offline configuration check.
Cookie conflict resolution is domain/path-aware. Credentials and token caches stay
local and are never included in the handoff.

Live source availability, authentication, rate limits, TLS trust and export permissions
remain external requirements. No full crawl or successful live-fetch claim is implied
by offline tests. Synthetic tests cover the complete pipeline without these services.
