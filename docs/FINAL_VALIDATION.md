# Final standalone live validation — 2026-10-06

## Evidence and execution boundary

Branch: `standalone/scraping-pipeline`. Implementation baseline:
`197aa2d94920d356128393070a73656c903ec3c8` (Grafana backport).
The full run wrote only to `runtime/final-live-20261006-0436`; prior runtime
artifacts were protected and their hashes remained unchanged. Duration:
1,056.797 seconds (about 17 minutes 37 seconds). No ingestion integration bug
requiring a code change was identified.

Production was enforced: `APP_ENV=production`, `IDMC_VERIFY_TLS=true`, HTTP used
the trusted local `certs/idmc-ca-bundle.pem`, and browser HTTPS verification was
not disabled. Chromium was installed independently under
`runtime/playwright-browsers`; browser temporary files used
`runtime/looker-browser-temp`. Python 3.13 and the pinned dependencies were used.
No experiment browser/runtime, embeddings, vector database or RAG was invoked.

Local ignored evidence includes `extraction_report_v2.json`,
`final_validation_report.json`, `validation_run.json` and
`cctv_pagination_diagnostic.json` in that directory. These are run artifacts, not
required test fixtures or committed production datasets. This document preserves
the reviewed results for a clean checkout; future live counts may differ.

## Portal coverage and run metrics

Nine active menus: Dashboard Ajimandaya; Dashboard IKM dan Perkebunan;
Dashboard JSP; Dashboard Kepegawaian; Dashboard Koperasi & UKM; Dashboard Pangan;
Dashboard Surveillance; Manajemen SPBE; Social Media Analytic. Beranda's home
mapping is additional to the nine menu groups.

| Metric | Result |
| --- | ---: |
| Mapped sources / unmapped visible entries / hidden inventory excluded | 25 / 0 / 7 |
| Targets discovered | 255 |
| Attempted / succeeded / failed | 97 / 93 / 4 |
| Skipped / unavailable | 158 / 0 |
| Carried forward in this fresh live directory | 0 |
| Metadata-only records | 1 |
| Records / rows / chunks | 97 / 5,550 / 2,436 |
| Lost / duplicated row positions / duplicate chunk IDs | 0 / 0 / 0 |
| Oversized indivisible-row chunks | 12 |
| Standalone tests after live run | 183 passed |

Final status: **PARTIAL**, exit code **2**, not an aborted run. Mapping is complete
for the observed scope, but structured/visual coverage is not. The 158 skips are
155 unsupported targets (Looker 134, Tableau 2, Grafana 19) plus three known-bad
Superset targets. Counts distinguish targets from records: Grafana panels can
produce multiple series records, and the legacy metadata-only result counts as
successful target execution without extracted rows.

Hidden sources excluded: Dashboard Kependudukan, Pertanahan V2, Profil Tenaga
Kesehatan, Profil Rumah Sakit, Profil Puskesmas, Profil Fasilitas Puskesmas and
Demografi Pelajar DIY. All published records passed current visibility/provenance
checks. See the [mapping walkthrough](SOURCE_DISCOVERY_WALKTHROUGH.md).

## Results by adapter

| Source type | Successful targets | Failed | Skipped | Records | Rows | Chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `superset` | 51 | 2 | 3 | 51 | 1,354 | 99 |
| `cctv_native` | 9 | 2 | 0 | 9 | 781 | 172 |
| `looker_studio` | 18 | 0 | 134 | 18 | 909 | 266 |
| `tableau_public` | 1 | 0 | 2 | 1 | 3 | 1 |
| `grafana_public` | 13 | 0 | 19 | 17 | 2,503 | 1,897 |
| Legacy `tableau_looker` | 1 | 0 | 0 | 1 | 0 | 1 |

Looker table results:

| Report | Supported tables | Rows | Unsupported components |
| --- | ---: | ---: | ---: |
| IKM | 1 | 6 | 0 |
| SPBE 2023 | 2 | 68 | 42 |
| SPBE 2024 | 5 | 215 | 29 |
| SPBE 2025 | 5 | 405 | 29 |
| Masterplan JSP | 5 | 215 | 34 |

Successful native CCTV rows by location: ATCS 148, Bantul 52, Kominfo Sleman 123,
Kominfo Gunungkidul 176, KP 59, Public 79, Sleman 34, Sungai 25, UPT Malioboro 85.
Fiber Optic yielded three `metric / display_value / unit` rows. Grafana queried
seven supported Yogyakarta and six Mudik panels; one valid structured no-data
record was retained explicitly.

## Source-level limitations and failures

| Source | Observed limitation | Output treatment |
| --- | --- | --- |
| UMKM / Superset | Chart 1048 (Top Product Listing) and 1093 (Sebaran Penjualan berdasar Kategori) timed out after retries | Two classified timeout failures; other charts continue. Prior successful rows can be carried stale on a later run |
| CCTV SPL | Five pages, 467 returned positions but only 288 unique camera IDs; 179 repeated ID positions across pages | Validation failure; incomplete group not published |
| CCTV Kota | Three pages, 273 returned positions but only 174 unique camera IDs; 99 repeated ID positions across pages | Validation failure; alias is verified, completeness is not |
| Perkebunan | Existing legacy path provides public metadata, not a structured table | `metadata_only`, zero rows, one explicitly labelled chunk; not represented as numeric extraction |
| Looker non-table visuals | 134 components lack supported structured table semantics; Masterplan's headerless accessibility representation is `simple-sankey` | Explicit unsupported/skipped entries, no guessed columns or chart approximations |
| Tableau details | Both worksheets' summary-reader requests returned 403 PermissionDenied; direct CSV routes returned 404 | Only three publisher-authored visible cards supported; worksheets skipped, `is_complete=false` |
| Grafana custom panels | Custom ECharts/dynamic-text and client-transform chains do not have supported equivalent output semantics | Nineteen explicit unsupported panel targets; no plugin JavaScript or fabricated rendered values |

Failure categories were two `timeout` and two `validation`. CCTV page totals did
not change in the diagnostic: cross-page overlap was the observed discrepancy.
The remote backend's exact cause remains unproven; bypassing unique-ID/total checks
would conceal incomplete data. Known-bad Superset charts 1038/1115 and text/empty
query target 1459 were skipped by configuration.

Looker coverage is configured landing-page tables, not every report tab/filter.
Tableau cards are not claimed to respond to worksheet filters or represent route
details. Grafana has no cursor pagination, saved query/top-N limits remain, and
requested absolute times are not confirmed backend times when public time
selection is disabled. Dashboard-config update timestamps are not data freshness.

## Determinism, recovery and integrity checks

- Canonical staging reconstruction and chunk replay were byte-identical to the
  captured outputs, including replay from reversed record-map insertion order.
- Delta replay detected zero content/metadata changes and no regeneration need.
- Validation reconstructed every accepted record's canonical rows: no lost or
  duplicated positions, bad ranges or duplicate chunk IDs. Legitimate equal values
  and cross-source overlaps were preserved; rejected remote groups are not included
  in this guarantee. The 12 oversized rows stayed intact and flagged.
- The fresh live run had no previous snapshot, so actual live carry-forward was
  zero. A separate **offline simulation using captured records** failed the ATCS
  target while healthy targets succeeded: one record was carried stale with identical
  rows and unchanged successful-fetch time; its check time advanced.
- An all-target-failed simulation aborted and preserved previously valid data.
  Original live outputs and prior normal-runtime artifacts were not overwritten.
- Full standalone suite afterward: `python -B -m unittest discover -s tests -v`,
  **183 passed**, including six browser DOM tests (no skips). `git diff --check`
  was clean.

Replay used identical received input, not a second time-moving full live crawl.
Passing integrity tests establishes accepted-output correctness, not complete
underlying source access. Offline reproduction commands and TLS/browser setup are
in [the operating contract](INGESTION_V2.md) and [README](../README.md).

## Documentation handoff verification — 2026-10-07

The documentation-only handoff reran the standalone suite: **183 tests passed**
in 70.395 seconds, including browser DOM checks. The production/custom-CA offline
configuration diagnostic passed without HTTP requests. Both synthetic reproductions
were byte-identical across their two runs with validated row coverage: base fixture
two records/151 rows/37 chunks; portal fixture five records/40 rows/nine chunks.
Local documentation links and `git diff --check` passed. These checks used isolated
`runtime/docs-handoff-20261006/` subdirectories (directory label only); no new live
crawl or source/configuration behavior change was performed.
