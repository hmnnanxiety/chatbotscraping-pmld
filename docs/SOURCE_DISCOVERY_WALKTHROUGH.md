# Technical walkthrough: discovering and mapping IDMC sources

This records the verified investigation and mapping used by the final standalone
run on **2026-10-06**. It is not a promise that remote menu labels, IDs or viewer
internals never change. Configured identities are in `ingestion_sources.json` and
`dashboards.py`; `portal_scope.py` rechecks visibility at each run. No runtime
snapshot or credentials need to be committed to reproduce synthetic tests.

## 1. Establish portal scope before inspecting source inventories

Inspect the same metadata the frontend uses:

- `GET https://idmc.jogjaprov.go.id/backend/api/v1/menus`
- `GET https://idmc.jogjaprov.go.id/backend/api/v1/config`

Menu entries expose names, IDs, hierarchy, active status and transport/source
identities. Scope requires an active entry **and all its ancestors**. The site's
`data[0].dashboard_url` supplies the separate home dashboard. Match these identities
to configured source URLs/keys and explicit aliases; do not infer mapping from
similar names or assume every scrapeable historical source is still visible.

The final scope contained nine active menu groups, 25 mapped sources (including
Beranda), no unmapped visible entries and seven excluded historical sources.
Mapped counts include a metadata-only source and sources with failed/unsupported
targets; they are not successful-extraction counts.

### Verified portal-to-source mapping

Superset numeric IDs below are configured IDs, except CCTV, which is resolved
from its embedded bootstrap at runtime. Portal page IDs are preserved separately.

| Portal menu → page (page ID) | Internal source identity | Platform |
| --- | --- | --- |
| Beranda → Beranda (`home`) | Dashboard Pembangunan; UUID `27b5d07e-111a-449a-84a2-efc3b6cf5fa1`; numeric `14` | Superset |
| Dashboard Kepegawaian → Dashboard Simpeg (`226`) | Dashboard Simpeg (Kepegawaian); `b575b146-15c6-4521-906d-dcc5fa5bed1e`; numeric `13` | Superset |
| Dashboard Koperasi & UKM → UMKM Marketplace (`185`) | UMKM Marketplace; `6d816cf9-91b5-4711-9b77-6ee93aa7bcca`; numeric `37` | Superset |
| Dashboard Pangan → Harga Pangan DIY (`81`) | Harga Pangan DIY; `380d5b59-7fad-4d96-9bbc-c0e8c0309e41`; numeric `69` | Superset |
| Dashboard Surveillance → Dashboard CCTV (`207`) | Dashboard CCTV (Surveillance); `e5f3e11f-f456-4729-aa68-53e53c1e459e`; numeric ID resolved at runtime | Superset |
| Dashboard Surveillance → cctv atcs (`23`) | `cctv.atcs` → `cctv-atcs` | CCTV native |
| Dashboard Surveillance → cctv bantul (`25`) | `cctv.bantul` → `cctv-bantul` | CCTV native |
| Dashboard Surveillance → cctv kominfo sleman (`31`) | `cctv.kominfosleman` → `cctv-kominfosleman` | CCTV native |
| Dashboard Surveillance → cctv kominfogk (`202`) | `cctv.kominfogk` → `cctv-kominfogk` | CCTV native |
| Dashboard Surveillance → cctv kp (`26`) | `cctv.kp` → `cctv-kp` | CCTV native |
| Dashboard Surveillance → cctv public (`27`) | `cctv.public` → `cctv-public` | CCTV native |
| Dashboard Surveillance → cctv sleman (`28`) | `cctv.sleman` → `cctv-sleman` | CCTV native |
| Dashboard Surveillance → cctv spl (`201`) | `cctv.spl` → `cctv-spl` | CCTV native |
| Dashboard Surveillance → cctv sungai (`29`) | `cctv.sungai` → `cctv-sungai` | CCTV native |
| Dashboard Surveillance → cctv uptmalioboro (`30`) | `cctv.uptmalioboro` → `cctv-uptmalioboro` | CCTV native |
| Dashboard Surveillance → cctv kota (`24`) | Explicit alias `cctv.atcs-kota` → `cctv-kota` | CCTV native |
| Dashboard IKM dan Perkebunan → IKM (`165`) | DATAKU-IKM; config `looker-ikm` | Looker Studio |
| Dashboard IKM dan Perkebunan → Perkebunan (`164`) | Perkebunan DIY; config `looker-perkebunan-diy` | Looker via legacy `tableau_looker`, metadata-only |
| Manajemen SPBE → Arsitektur dan Peta Rencana SPBE 2023 (`187`) | Arsitektur dan Peta Rencana SPBE DIY 2023; `looker-spbe-2023` | Looker Studio |
| Manajemen SPBE → ARSITEKTUR SPBE DIY - 2024 (`194`) | Arsitektur dan Peta Rencana SPBE DIY 2024; `looker-spbe-2024` | Looker Studio |
| Manajemen SPBE → Arsitektur SPBE DIY 2025 (`200`) | Arsitektur dan Peta Rencana SPBE DIY 2025; `looker-spbe-2025` | Looker Studio |
| Dashboard JSP → Masterplan Jogja Smart Province (`173`) | Masterplan Jogja Smart Province; `looker-jsp` | Looker Studio |
| Dashboard Ajimandaya → Jaringan di DIY (`171`) | Dashboard Jaringan Fiber Optic di DIY; `tableau-jaringan-diy` | Tableau Public |
| Social Media Analytic → Yogyakarta (`188`) | Monitoring Media Analytics Yogyakarta; `grafana-sma-yogyakarta` | Grafana/xPlore |
| Social Media Analytic → Mudik (`189`) | Monitoring Media Analytics Mudik Yogyakarta; `grafana-sma-mudik` | Grafana/xPlore |

The nine menu names are the nine non-Beranda groups in this table. Excluded
inventory names were Dashboard Kependudukan, Pertanahan V2, Profil Tenaga Kesehatan,
Profil Rumah Sakit, Profil Puskesmas, Profil Fasilitas Puskesmas and Demografi
Pelajar DIY. Their existence/accessibility is not permission to publish them under
current frontend scope. Preserve source names/IDs; do not replace them with portal
names destructively.

## 2. Superset: resolve viewer identity, then reproduce effective queries

Portal sources use `superset:https://dwh.jogjaprov.go.id|<embedded UUID>`.
The UUID identifies the embedded view, not the numeric API dashboard ID. Inspect
`https://dwh.jogjaprov.go.id/embedded/<uuid>` bootstrap JSON when the numeric ID
is absent from config; the current adapter can resolve it. The historical registry
docstring still describes a manual-only resolver: use `SupersetAdapter.discover()`
as the current implementation authority, not that outdated comment.

Source session cookies and dynamic guest tokens are handled in `dwh_client.py`.
The portal guest-token endpoint is
`https://idmc.jogjaprov.go.id/backend/api/v1/superset/guest-token`; tokens are
dashboard-specific, refreshed and kept out of output. Simpeg needed the actual
portal referer configured in `dashboards.py`: a generic referer yielded HTTP 422.
Cookie fallback validity must be tested against the source, not inferred from
environment-variable presence.

On `https://dwh.jogjaprov.go.id`, discovery/extraction uses:

1. `GET /api/v1/dashboard/{numeric_id}` and chart inventory.
2. `GET /api/v1/chart/{slice_id}` for stored `query_context`/metadata.
3. `POST /api/v1/chart/data` with each query's effective dashboard defaults.

A chart's stored query alone is not necessarily what the user sees: applicable
year/filter defaults and dashboard filter scope must be included without duplicate
filters. Unknown forms fail instead of pretending an unfiltered result is equivalent.
Multi-query results preserve distinct result indices and validate the returned set.

Validation used mocked auth/query/filter cases and live scoped chart extraction.
Final Superset output had 51 records/1,354 rows; UMKM charts 1048 and 1093 timed
out despite retries. Known-bad targets 1038/1115 and empty query/text block 1459
were skipped. Failure isolation, not source-specific fabricated rows, kept other
charts available.

## 3. CCTV: verify location filters and full pagination

The native endpoint is `https://idmc.jogjaprov.go.id/backend/api/v1/cctv`.
Use `filter[location]=<configured slug>`; the mapping table shows actual slugs.
Kota demonstrated why transport aliases cannot be blindly converted: its portal
source `cctv.atcs-kota` maps to **`cctv-kota`**, not `cctv-atcs-kota`.

Follow response `links.next` until exhaustion with allowed origin/path/filter,
cycle/page and returned-location checks. Compare deduplicated camera IDs against
advertised totals; the initial 100-row page is not the entire group. Fixture tests
exercise multi-page traversal, mismatches, hostile links and aliases.

Final live output: nine groups/781 cameras. SPL and Kota returned overlapping IDs
across pages despite internally consistent page totals: SPL 467 positions but 288
unique IDs; Kota 273 positions but 174 unique IDs. They were rejected as validation
failures. The exact remote backend cause is unproven; dropping overlaps and calling
the remainder complete would be a wrong assumption.

## 4. Looker: preserve report/page identity and table positions

Public URLs use `https://lookerstudio.google.com/embed/reporting/<report UUID>`
and, when exposed/configured, `/page/<page ID>`:

| Portal page | Report UUID | Configured page |
| --- | --- | --- |
| IKM | `ce80e599-db1e-4cb5-8c7e-b6bd64e9e81a` | `siyYD` |
| SPBE 2023 | `e71730f5-9086-4f2f-aa9d-e3d57ff7b4d3` | `p_r0amzb8w1c` |
| SPBE 2024 | `ca918e9c-851d-4152-bd28-525bdfedcc17` | No explicit page; landing binding, no invented page ID |
| SPBE 2025 | `4cfe1526-1ac6-403b-8bfd-dcdbdf15c78d` | `X94cD` |
| Masterplan JSP | `feffa0ab-1727-484e-89bd-979871799ae6` | `p_xm5zudawmd` |
| Perkebunan (legacy metadata-only) | `2f568f00-d9a0-4f99-a1fe-6df3ff503927` | `p_9s7xethl8c` |

The reusable adapter opens a fresh isolated public browser context, discovers
stable `cd-*` components and extracts rendered table structure, not screenshots,
OCR or undocumented signed-in Google RPCs. Pagination ranges/totals and unchanged
columns define coverage. Virtualized rows require stable positions; equal cell
values cannot be used as a deduplication key.

The major parser finding was recycled slot IDs: ordinal-free tables expose native
`block-N index-M` coordinates. Tracking the entire block/slot pair, checking
contiguity and merging pinned segments fixed multiple SPBE tables without report
hacks or pixel-stride guesses. Masterplan's supposedly headerless table
`cd-twjqcvpizd` was a `simple-sankey` accessible representation, not a supported
table; guessed headers would misrepresent it.

Synthetic fixtures/local DOM tests cover these cases; targeted independent browser
sessions checked fixed-clock normalized replay. Final live extraction succeeded
for all 18 supported tables/909 rows across five reports. The 134 other visual
components remained explicit unsupported targets. This does not crawl all report
pages or manipulate interactive filters. See [Looker details](LOOKER_INGESTION.md).

## 5. Tableau Fiber Optic: visible cards are not worksheet details

Portal Jaringan di DIY points to
`https://public.tableau.com/views/DashboardJaringanDIY/FO` (display-only query
parameters are present in config). Workbook **DashboardJaringanDIY**, view **FO**
are source identity, not aliases for a complete underlying geometry dataset.

Live inspection via the official Embedding API found `Sheet 1 (3)` and `Sheet 11`.
Direct CSV routes returned 404; `getSummaryDataReaderAsync()` returned 403
`PermissionDeniedException`. These results ruled out reliable detailed export;
permissions were not bypassed.

The initialized viewer's ordinary
`/vizql/w/DashboardJaringanDIY/v/FO/bootstrapSession/` response exposes publisher
text-zone captions. The reusable parser handles framed structured responses
(lengths count UTF-16 code units, not UTF-8 bytes), binds visible zones and checks
captions against DOM text. Hidden workbook tabs do not imply hidden worksheets
inside an active dashboard; actual zone/API membership supplies identity.

Extract only the configured three labels, **Panjang Kabel Udara**, **Panjang Kabel
Tanam**, **Panjang Agregat**, with explicit unit `m` and original display strings.
Do not sum map features, infer locale separators or claim that cards follow
worksheet filters. Output fields are `metric`, `display_value`, `unit`, with
`extraction_mode=visible_metric_cards`, `is_complete=false` and separate
`metric_cards_complete=true`. Two real worksheets are reported unsupported.

Synthetic framing/zone/permission tests and independent live browser sessions
validated the path; final output was one record/three rows/one chunk. Full
Tableau worksheet support is not claimed. See [Tableau details](TABLEAU_INGESTION.md).

## 6. Grafana/xPlore: public definitions and saved panel queries

Both sources share origin `https://xplore.pustakadata.id`:

| Portal page | Public dashboard ID | Internal dashboard UID |
| --- | --- | --- |
| Yogyakarta | `9bd0711386f5414a8d408124231d7209` | `b9705ea9-6863-4bd1-98c0-d142f9338f89` |
| Mudik | `dfe21823dc1e4d05b789d9a97f831c89` | `aegl4agfxuqrkd` |

Starting from `/public-dashboards/<public ID>`, inspect the public definition:
`GET /api/public/dashboards/{public_id}`. Supported panels use read-only
`POST /api/public/dashboards/{public_id}/panels/{panel_id}/query` with
`intervalMs`, `maxDataPoints` and `timeRange`. It queries server-saved targets;
private datasource credentials, arbitrary queries and plugin JavaScript are not
required or executed. Public IDs and internal UIDs are distinct and both retained.

Normalize typed columnar DataFrames after checking refs, schema, vector lengths,
statuses and identity. Support core timelines, categorical all-values pies,
untransformed tables, geomap query rows and unfiltered sum-stat aggregates.
Preserve series labels/geohashes and explicit no-data results. An HTTP 200 body
can still wrap a query failure; do not treat transport success as valid data.

Raw frames are not always rendered values: the client-transformed sentiment table
cannot safely be replaced by raw `site.keyword / sentiment.keyword / Count`.
Custom ECharts/dynamic-text panels are explicitly unsupported. The endpoint has no
pagination cursor; artificial time-window splitting changes aggregation semantics.
Saved `now-7d` → `now` is recorded, but public sharing can ignore a requested
absolute range. `meta.updated` describes dashboard configuration, not source-data
freshness. These are important limits, not fields to fill with plausible guesses.

Synthetic semantic/query tests, targeted live panel queries and deterministic
replay validated 13 supported panels (seven Yogyakarta, six Mudik). The final full
run produced 17 records/2,503 rows/1,897 chunks and 19 unsupported panel targets.
Time-moving public data explains differences from earlier targeted counts; replay
uses identical captured input, not another live crawl. See [Grafana details](GRAFANA_INGESTION.md).

## 7. Handoff validation and future maintenance

The final standalone run used production TLS/custom CA, an independent local
Chromium cache and a fresh isolated output directory. Whole-row replay, delta,
portal provenance, stale-recovery simulation and abort preservation were verified;
183 tests passed. See [full validation evidence](FINAL_VALIDATION.md).

When portal state changes, first inspect current menu/config metadata, compare
exact source identities, and report unmapped/hidden entries. Confirm extractable
structured data before adding a mapping or adapter. Add minimal synthetic fixtures
for any parser change, test fixed-input replay and run a targeted isolated validator
before a full run. Keep unsupported/no-data/metadata-only distinctions explicit;
never enlarge coverage claims to hide an upstream limitation.
