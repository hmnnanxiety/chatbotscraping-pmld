# Architecture and operating contract, revision 2.1.0

## Purpose and repository boundary

This is a standalone reusable scraping/ingestion system for public IDMC DIY
dashboard results. The logical flow is source → discovery → extraction →
normalization → validation → delta/change detection → staging → chunking → JSON.
Source permissions, visible filters and advertised coverage constrain extraction;
an accessible page does not imply an accessible underlying dataset.

The project stops at output. It does not embed, create a vector database, retrieve,
run RAG, generate answers or serve chat. No sibling-project imports, FastAPI,
Gemini, Chroma or production snapshots are required. The flat Python structure is
intentional; this handoff does not reorganize working modules.

## Module ownership

| Files | Responsibility |
| --- | --- |
| `main_orchestrator.py` | Startup, lock/recovery, scoped extraction, delta, validated bundle publication, report and exit status |
| `ingestion_config.py`, `.env.example` | Process-local environment, TLS/custom CA policy, safe configuration diagnostics |
| `portal_scope.py` | Current menu/home discovery, identity matching, visibility gating and portal/source provenance |
| `ingestion_sources.json`, `dashboards.py` | Known inventory, aliases, report URLs, Superset UUID/numeric IDs and referer overrides |
| `extractors/base.py`, `extractors/registry.py` | Lightweight adapter contract, configured factory/inventory, reusable HTTP/retry transport |
| `extractors/superset.py`, `dwh_client.py` | Superset bootstrap, dashboard/chart queries, session cookies and dynamic guest tokens |
| `extractors/cctv.py` | Native CCTV location API and guarded pagination |
| `extractors/looker_studio.py`, `looker_browser.py`, `looker_dom.js` | Looker table catalogue, isolated browser and reusable DOM/virtual-row parser (all under `extractors/`) |
| `extractors/tableau_public.py`, `extractors/tableau_browser.py` | Tableau public-view initialization and visible structured metric-card extraction |
| `extractors/grafana_public.py` | Public dashboard/panel discovery, supported query semantics and typed DataFrames |
| `extractors/tableau_looker.py` | Legacy explicit metadata/export path, currently visible Perkebunan metadata-only |
| `contracts.py`, `extractor.py` | Source-neutral records, normalization/validation, retries, failures and carry-forward |
| `delta_checker.py`, `etl_common.py` | Separate hashes, serialization, locks, atomic writes and recoverable publication |
| `transformers/`, `transformer.py` | Staging migration, whole-row chunk generation/coverage validation and offline CLI |
| `tests/`, `docs/`, `requirements*.txt` | Synthetic tests, optional targeted validators, reproducible dependencies and handoff notes |

An adapter implements `source_type`, `discover() -> list[SourceTarget]` and
`extract(target) -> list[NormalizedRecord]`. Optional `adapter_id` and
`target_prefixes` identify previous records it owns for recovery. A target can
produce several results, each with a distinct `result_index`. Authentication,
query semantics and source-specific completeness stay inside the adapter, not
the generic chunker. New adapters should reuse this small boundary, not invent a
parallel ETL framework.

## Frontend-aware scope

Every normal configured run first obtains verified portal menus and home config.
Only active entries with active ancestors are matched against the source inventory;
the home dashboard is matched separately. Matching uses transport/source identity
and explicit aliases, not fuzzy display-name similarity. The source inventory may
contain hidden dashboards without publishing their records.

The scope wrapper attaches `portal_menu_name`, `portal_page_name`,
`portal_page_id`, `portal_path`, `portal_visible`, `source_dashboard_name` and
`source_chart_name`, while retaining source type, IDs and URL. Chunk headers prefer
portal-facing names; internal identity remains available for extraction/debugging.
Hidden previous records are removed before carry-forward and filtered again before
publication. `build_documents` rejects an explicitly hidden record.

A failed/unverifiable portal lookup aborts; it does not fall back to the full
inventory. A verified scope with no configured matches can publish an empty result.
Unmapped visible entries are reported and make the run partial. Counts such as
the last observed nine menus are evidence, not a hardcoded rule. See the
[verified mapping walkthrough](SOURCE_DISCOVERY_WALKTHROUGH.md).

## Normalized schema and provenance

Staging envelope: `schema_version = 2`, `normalization_version = "2.1.0"`.
Transform revision: `transform_version = "2.1.0"`. Records are indexed by
`record_id`, which is derived as `target_id + ":result" + result_index`.
`SourceTarget.id` combines source type, scope/dashboard identity and chart ID;
Superset uses its UUID scope even when its API needs a numeric dashboard ID.

```json
{
  "record_id": "example:dashboard:chart:result0",
  "target_id": "example:dashboard:chart",
  "source_type": "example",
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
    "last_checked_at": "2026-10-06T04:36:00+00:00",
    "last_successful_fetch_at": "2026-10-06T04:36:00+00:00",
    "stale": false,
    "is_complete": "unknown",
    "extraction_mode": "api"
  }
}
```

This generic example omits the portal fields; a portal-scoped record includes the
complete portal/source name set described above. Metadata can also carry columns,
report/page/panel/worksheet identity, default filter evidence, query limits, renderer
type and coverage notes. Unknown period, unit and publication date remain null;
the run clock is not a substitute for source freshness. `source_update_date` may
retain a source-reported text date with unknown timezone. Checked/fetched timestamps
must be timezone-aware ISO-8601; null is allowed for stale legacy snapshots, not
fresh records. `is_complete` is boolean or `"unknown"`, never a guessed guarantee.

Validation rejects inconsistent IDs/map keys, missing provenance, invalid metadata
and row shapes, non-JSON values, NaN/infinity, malformed observation timestamps and
fetch dates later than check dates. `metadata_only` requires zero data rows. Empty
structured query results are allowed but remain distinguishable from metadata-only.

## Adapter responsibilities and supported boundaries

- **Superset:** session/fallback cookies, guest-token refresh, numeric bootstrap
  resolution, dashboard/chart discovery and stored `query_context`. Apply effective
  dashboard defaults (including scoped year filters) to each applicable query;
  unknown filter forms or incomplete query-result sets fail explicitly.
- **CCTV native:** location filters and `links.next` traversal, allowed origin/path,
  retained location filter, cycle/page bounds and camera-ID/total consistency. Never
  declare the first 100 rows a complete multi-page group. Conflicting snapshots or
  deduplicated totals below the advertised count fail rather than publish silently.
- **Looker Studio:** catalogue tables on the configured page, capture displayed
  columns/strings, traverse table pagination and virtual rows with complete position
  coverage. Preserve equal rows; reject unstable columns/totals/coordinates.
  Non-table visuals are explicit unsupported targets, not metadata-only success.
- **Tableau Public:** the configured Fiber Optic cards in visible bootstrap text
  zones, checked against the initialized viewer DOM. Workbook/view and real worksheet
  identities remain metadata. No denied underlying/detail export is bypassed.
- **Grafana/xPlore:** discover saved public panels and query only server-saved
  targets; normalize strict typed DataFrames and supported visible aggregates.
  Unsupported renderers/transformations are reported, not executed or approximated.
  Preserve saved time-range policy and explicit successful no-data results.
- **Legacy Tableau/Looker:** public metadata or configured same-origin exports;
  visible Perkebunan currently has no structured rows and stays `metadata_only`.

Detailed limits: [Looker](LOOKER_INGESTION.md), [Tableau](TABLEAU_INGESTION.md),
[Grafana](GRAFANA_INGESTION.md). Current failures are recorded in
[final validation](FINAL_VALIDATION.md).

## Reliability, stale data and publication

Discovery and each target have retry/backoff for transient failures (three attempts
at the generic boundary), not permanent HTTP errors or certificate failures.
Classified failures are isolated; healthy targets continue. Known-bad targets are
skipped by default. `--include-known-bad` also forces targets marked unsupported
to execute and fail explicitly; it is a diagnostic override, not normal operation.

For still-visible sources, previously successful rows can survive discovery/target
failure or missing targets as `stale=true`. Carry-forward updates the check/error
metadata but preserves `last_successful_fetch_at` and rows. Newly hidden data is
excluded, not carried forward. The default minimum success ratio is 0.5 and counts
unavailable targets from failed discovery; three distinct source/dashboard scopes
in a consecutive systemic-failure streak trigger an abort. Healthy success or
non-systemic failure breaks the streak. A wholly failed extraction does not publish
over successful data, even if the configured threshold is permissive.

Normalized records and chunks validate before any known-good artifact is replaced.
The orchestrator computes delta and builds chunk output in memory, then prepares
all payloads together: staging is not published early as an unvalidated intermediate.
Prepared files are individually atomically replaced, delta state last. A journal
and backups support rollback and next-run recovery after interruption; a kernel
lock prevents concurrent writers in one output directory. This is **not** a
cross-file database transaction for unlocked readers: consume after job completion
or coordinate with the same lock. Abort reports/logs may update while prior
staging/chunks/delta stay unchanged.

## Delta and deterministic chunking

Normalization sorts object keys and rows by canonical JSON. This is deliberate
canonical order, not a promise to retain the viewer's original sort. Duplicate
input values remain distinct row positions. Same full input/configuration gives
identical normalized records, grouping, text, IDs and hashes. Real live check times
and changing public data may differ; byte identity across different live crawls is
not promised.

Content hashes include identity, rows, semantic schema/names, portal labels,
source URL, period, units, effective filters, mode and completeness. Observation
timestamps/stale/error metadata do not create false content changes. Metadata hashes
track all metadata separately, including freshness. Version/config changes force
regeneration; deleted IDs are detected too. `DeltaChecker.check()` does not commit
state; the orchestrator publishes it only after staging/chunk generation succeeds.
The inexpensive chunk generation still runs on unchanged/metadata-only runs to
refresh metadata and repair missing files; no embedding operation exists here.

Default readable budget: 1,500 characters, configurable with `--max-chunk-chars`
(minimum 256). Header context and row-range text count against the budget. Rows are
serialized intact; a small dataset stays together when it fits. A single long row
may exceed the budget only with `oversized_row=true`, `partial_row=false`; no row
is truncated. Context alone exceeding the budget fails clearly. IDs are
`v2-{md5(record_id)}-chunk{zero_based_index}` and content hashes are MD5 of readable
text, deterministic change identifiers rather than security signatures.

Every chunk has source/dashboard/chart and brief period/unit/filter context,
provenance, record/target IDs, `row_start`, `row_end`, `total_rows`, `chunk_index`,
`chunk_count`, budget/order/version and hash. Row ranges are one-based inclusive;
empty/metadata-only records have one explicit zero-row chunk with range 0–0.
Technical metadata is not dumped into the text. Chunk metadata omits nulls and
serializes nested objects/lists as canonical JSON strings for compatibility;
staging retains their structured types. Consumers should not treat a zero-row
metadata chunk as extracted table data.

Coverage validation reconstructs all serialized rows and compares their content,
order and multiplicity; it also checks contiguous ranges, unique IDs, counts,
context, provenance and budget flags. Zero loss/duplication means positions within
each record, not deduplication across different sources or proof of all remote rows.

## Configuration and TLS

| Input | Meaning |
| --- | --- |
| `ingestion_sources.json` | Adapter/source lists, explicit URLs, aliases and visible-card labels/units |
| `dashboards.py` | Superset UUID/numeric IDs, source names and source-specific token referer overrides |
| `.env.example` / local `.env` | Example only / local secrets and environment; never replace an existing `.env` blindly |
| `APP_ENV` | `development` (unset default) or `production` |
| `IDMC_VERIFY_TLS` | Verified HTTPS by default; explicit false is development-only |
| `IDMC_CA_BUNDLE` | Readable trusted PEM bundle, absolute or project-relative; overrides ordinary HTTP trust |
| `DWH_COOKIE_APP_SESSION`, `DWH_COOKIE_LARAVEL_SESSION`, `DWH_COOKIE_XSRF_EXTRA`, `DWH_COOKIE_XSRF_TOKEN` | Optional Superset fallback cookies; presence checks do not establish validity |
| `PLAYWRIGHT_BROWSERS_PATH` | Optional local browser cache; use consistently for installation and execution |

Environment loads once before clients; process variables override `.env`. Restart
after changes. `.env.example` explicitly disables TLS in development: set production
and true before deployment. Production rejects explicit false even with a CA file.
Development-only insecure HTTP suppresses just `InsecureRequestWarning` and emits
an application warning. Certificate failures should be fixed with trusted CA setup,
not a production bypass. Never publish cookies, guest tokens or raw session payloads.

Requests uses the configured CA bundle. Chromium always checks platform TLS trust
with `ignore_https_errors=false`; HTTP PEM configuration does not install browser
trust. If needed, ask your administrator to install the trusted enterprise CA in the
platform store. Browser download tooling may separately require trusted CA setup
(for example `NODE_EXTRA_CA_CERTS`); this does not replace source TLS policy.
Local certificates, environments and caches must stay out of commits.

## Outputs and operation

Always specify `--output-dir runtime` or an isolated validation directory: the
historical CLI default is the project root. All ingestion outputs/logs, the lock
and transaction recovery files use that directory. The ignored Superset token
cache `.dwh_token_cache.json` remains at the project root; do not relocate it by
editing paths during handoff. Other source/browser caches are local runtime state.

| Output | Shape / purpose |
| --- | --- |
| `staging_idmc_data.json` | `{schema_version, normalization_version, records: {record_id: record}}` |
| `ready_for_vector_db_v2.json` | List of `{id, page_content, metadata}` chunks; no database or embedding call |
| `delta_state_v2.json` | Schema/normalization/transform/config versions/hashes plus per-record content/metadata hashes |
| `extraction_report_v2.json` | Status, portal mapping/exclusions, discovered/attempted/succeeded/failed/skipped/unavailable/carried/metadata-only counts, records/chunks, failure categories, delta summary and duration |
| `ingestion_errors.log` | Local classified diagnostics; review before sharing |

```powershell
python -B -m tests.smoke_config --output-dir runtime
python -B main_orchestrator.py --output-dir runtime --max-chunk-chars 1500
# Optional forced regeneration; no downstream services:
python -B main_orchestrator.py --output-dir runtime --force
# Offline transform of existing staging only:
python -B transformer.py --staging runtime/staging_idmc_data.json --output runtime/ready_for_vector_db_v2.json
```

Exit codes are `0` ok, `2` usable partial and `1` aborted. Check the report rather
than interpreting partial as full source coverage. Use the same output directory
for normal successive runs to enable delta/carry-forward; a fresh validation
directory intentionally has no previous data to recover. Old v1/experimental-v2
staging can be explicitly loaded/migrated without mutating the original file.

## Testing and reproduction

Install pinned HTTP dependencies with `requirements.txt`. Optional
`requirements-looker.txt` supplies Playwright for both Looker and Tableau; install
Chromium separately (`python -m playwright install chromium --only-shell`). Base
tests use synthetic fixtures; DOM integration tests use synthetic HTML in a local
browser with network disabled and can skip when no browser is available.

```powershell
python -B -m unittest discover -s tests -v
python -B -m tests.reproduce --output-dir runtime/reproduction
python -B -m tests.reproduce_frontend --output-dir runtime/frontend-reproduction
git diff --check
```

Each reproduction performs two fixed-clock runs and checks staging/chunks/delta
bytes plus whole-row coverage. The base fixture has two records/151 rows; the
portal-aware fixture uses five small Looker reports/40 rows. They are not production
snapshots. Tests cover source isolation, retries/error classification, systemic
abort, stale timestamps, rollback/recovery, delta/version changes, scope/aliases,
adapter parsers and chunk/schema integrity, not retrieval or LLM quality.

Optional targeted validators are `tests.validate_tableau_live` and
`tests.validate_grafana_live`; use `--help` for
their current options and isolated output paths. They require external network
and source access, unlike the automated acceptance suite. The last full standalone
live evidence is documented separately in [FINAL_VALIDATION.md](FINAL_VALIDATION.md),
not implied by passing offline tests.
