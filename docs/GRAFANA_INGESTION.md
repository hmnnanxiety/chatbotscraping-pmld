# Grafana/xPlore public SMA ingestion

The [final standalone full run](FINAL_VALIDATION.md) validated 13 supported panels,
17 records, 2,503 rows and 1,897 chunks, with 19 unsupported panels. The earlier
targeted experiment counts below are historical, not the final standalone totals.

One `GrafanaPublicAdapter` implements `discover()` / `extract(target)` for both
Yogyakarta and Mudik. Registry config uses `grafana_sources`; exact origin/public
URL matching keeps portal scope separate from internal dashboard UID/title.
Schema 2.1.0, existing normalization/chunking/publication and TLS/custom CA policy
are unchanged. No new dependency, browser, private API or credential is needed.

## Public mechanism

GET `/api/public/dashboards/{public_id}` discovers the saved dashboard/panels.
POST `/api/public/dashboards/{public_id}/panels/{panel_id}/query` is a read-only
query of **server-saved** targets. Payload contains `intervalMs`, `maxDataPoints`
and `timeRange` only, never arbitrary datasource SQL/Lucene or plugin JavaScript.
Official mechanism: [Grafana v12 public query handler](https://github.com/grafana/grafana/blob/v12.0.0/packages/grafana-runtime/src/utils/publicDashboardQueryHandler.ts)
and [backend query policy](https://github.com/grafana/grafana/blob/v12.0.0/pkg/services/publicdashboards/service/query.go).

No cursor or offset exists in this endpoint. Saved aggregation/top-N/raw-query
limits remain in force. `maxDataPoints` is a query hint, not a guaranteed row cap
or complete archive export. `is_complete=false` explicitly disclaims exhaustive
source coverage; `query_result_complete=true` means all returned frames/refs were
validated, not that every article was extracted. Do not emulate pagination by
splitting time windows: that can change the visible aggregation semantics.

## Supported and explicit limitations

- Strict typed columnar DataFrames: time (epoch milliseconds to UTC ISO), number,
  string, boolean and null. Unequal vectors, duplicate columns, query errors,
  unexpected/missing refs and ambiguous frame identities fail the entire target.
- Core time series, all-values categorical pies, untransformed tables and geomap
  query rows; geohashes are retained, not converted to fabricated coordinates.
- Core stat panels with unfiltered `sum` reduction; null-only values remain null.
  Reducer output is the visible aggregate, not a list of intermediate time bins.
- Frame/field series labels are attached to each row so chunk context survives.
  Frame order does not determine IDs; equal source rows are intentionally preserved.
- Text panels are non-data. Custom ECharts/dynamic-text renderers and client
  transformation chains are explicitly unsupported/skipped and reported. In
  particular, raw `site.keyword / sentiment.keyword / Count` must not be presented
  as the rendered sentiment table's Netral/Positif/Negatif columns.
- Unsupported date-math rounding, template variables, panel time overrides and
  repeated panels fail/skip rather than silently change query semantics.

The time window uses the saved dashboard default, frozen to one request anchor
per adapter. Grafana's public sharing policy may ignore client-supplied absolute
range when time selection is disabled. `requested_time_range` is therefore **not**
claimed to be the backend's confirmed effective range. Both current dashboards
use `now-7d` to `now`; output preserves that saved statistical period/timezone.
The public definition's `meta.updated` is a dashboard-configuration timestamp,
not data freshness; `source_update_note` makes this explicit. Successful-fetch
timestamps are managed by the unchanged generic orchestrator/carry-forward logic.

## Targeted checks only

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_grafana_public tests.test_portal_scope -v
.\.venv\Scripts\python.exe -m tests.validate_grafana_live --output-dir runtime/grafana-targeted
git diff --check
```

Live validation queries only supported panels in the two configured public SMA
dashboards, writes isolated validation artifacts, checks primary runtime hashes,
and validates deterministic replay and whole-row chunk coverage. Unsupported
panels make coverage PARTIAL; metadata-only results are never reported as extracted
data. The validator does not run full registry ingestion or any downstream service.
The initial standalone backport was verified offline without live ingestion;
the subsequent complete standalone live run is documented separately above.

## Historical experiment validation, 2026-10-06

The following evidence was obtained in the experiment repo before backporting,
not by a new standalone live run. Runtime snapshots/reports were not copied and
are not required by tests. Synthetic fixtures alone exercise the adapter.

Both source URLs matched active Social Media Analytic pages (188 Yogyakarta,
189 Mudik). All 13 supported panel queries succeeded, with no extraction errors:

| Page | Supported panels | Rows | Records | Coverage |
| --- | ---: | ---: | ---: | --- |
| Yogyakarta | 7 | 2,471 | 11 | PARTIAL: 9 unsupported panels |
| Mudik | 6 | 44 | 6 | PARTIAL: 10 unsupported panels |

Yogyakarta: sum stat 1 row (922 articles); sentiment timeline 255 rows/3 series;
emotion timeline 129 rows/3 series; total-news timeline 8 rows; Top Media 10 rows;
Top Issue 10 rows; geohash/location/news-summary map 2,058 rows. Mudik: sum stat
1 row (zero); sentiment timeline 29 rows; emotion timeline explicitly no data;
total-news timeline 8 rows; Top Media 1 row; Top Issue 5 rows. These independently
saved panel queries need not have identical filters/totals; do not invent missing
emotion data or reinterpret a returned zero as extraction failure.

The 17 records produced 1,906 chunks with deterministic byte-identical replay,
zero lost/duplicated row **positions**, and zero oversized-row chunks. Large map
rows include public source summaries/links and dominate chunk count; the existing
1,500-character budget and whole-row policy were not changed. This is not an
embedding/retrieval evaluation. Main runtime artifact hashes were unchanged.

Unsupported: custom ECharts sentiment distribution, Top 10 Locations, public
perception and four word clouds; transformed sentiment-by-media table; dynamic
latest-news list (both), plus negative-news list (Mudik). Text heading panels
37/38/39 are non-data. Detailed runtime reports remain in the experiment project.
Live snapshots were not repeated: only identical received input was replayed,
since public range policy/data can move with the clock. Grafana support is partial,
not complete dashboard coverage or a complete article archive.
