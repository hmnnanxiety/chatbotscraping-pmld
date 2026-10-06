# Frontend coverage: CCTV Kota and Looker Studio

`cctv_portal_aliases` maps portal transport aliases to existing native camera
locations. Kota uses `cctv.atcs-kota` in menus but `cctv-kota` in the API. The
native adapter and next-link traversal are unchanged. Both internal identity and
the original portal entry remain in provenance.

`looker_sources` explicitly opts five reports into `LookerStudioAdapter`.
Existing `web_sources` remain compatible; Perkebunan is not silently migrated.
The adapter uses a fresh headless browser context, not a private Google RPC or
the user's signed-in browser profile. It discovers stable `cd-*` components on
the configured portal landing page, including unsupported visual charts.

Supported extraction: rendered Looker tables and consistent accessible HTML
chart tables. Pagination and virtualized scrolling must cover every advertised
row position with unchanged columns/totals. Equal data rows are preserved (not
deduplicated by values). Strings retain displayed locale/formatting. Row-order
normalization, hashes and whole-row chunking use the existing core unchanged.
No filter controls or other report pages are automatically activated. This is
chart-result coverage, not a claim of complete underlying data/report coverage.

Internal report UUID, configured page ID, stable component ID, source config ID
and discovery catalog are metadata. A report URL without a page ID uses a
`landing` identity binding; `report_page_id` remains null if the viewer does not
expose an actual page ID. No page ID is guessed. Statistical period, unit and
source update date remain null unless source-reported; a missing value is not
filled from the execution clock.

Unsupported charts are explicitly listed in `skipped_targets` and make the run
partial. Reports with no supported table fail discovery as `unsupported`.
Missing Chromium is `browser_unavailable`; readiness failures are `timeout`;
changed columns/totals, missing virtualized rows and bad ranges are `validation`.
These five sources never return metadata-only records as extraction success.
Existing retry/failure isolation, stale carry-forward and publication guards
handle these outcomes. TLS/auth interstitials are not bypassed.

## Optional runtime

```
python -m pip install -r requirements-looker.txt
python -m playwright install chromium
```

Base tests/other sources do not need Playwright installed. Optional local DOM
integration tests use installed Chromium or Microsoft Edge with synthetic HTML,
and prohibit network requests. Chromium always verifies HTTPS against its
platform trust store. The existing HTTP custom CA configuration is unchanged;
the browser does not silently apply the HTTP PEM file or disable verification.
If enterprise Google TLS requires a custom CA, install that CA in the platform
trust store through your normal admin process. No automatic trust-store edits.

Fixtures are synthetic small chart results, not production snapshots. Their row
counts demonstrate deterministic extraction/pagination; they do not establish
fresh live counts or guarantee future DOM compatibility. Fiber/Tableau dynamic extraction and Grafana/xPlore
remain unconfigured and unsupported.

## Targeted table validation (2026-10-06)

Tables without display ordinals can expose native `block-N index-M` coordinates.
The parser merges pinned segments and tracks rows by the full block/slot pair,
not the recycled slot alone. Blocks and slots must be contiguous from zero,
coverage must match the advertised page range, and repeated coordinates must
retain identical cells. No pixel-height/stride inference or value deduplication
is used. Missing or unstable coordinates still fail clearly.

The formerly "headerless table" `cd-twjqcvpizd` in Masterplan JSP is actually
a `simple-sankey` visualization with a headerless accessibility table. It is
unsupported, not a failed data table. The generic catalogue requires exposed
column headers before treating an accessible HTML table as supported. No Sankey
extraction, guessed column labels, or report-specific workaround is implemented.

Live checks are restricted to the five previously failed SPBE table components;
they do not establish complete report/non-table coverage. Browser runtime/cache
is under `runtime/playwright-browsers` on E: because C: lacked installation space;
the validation process sets `PLAYWRIGHT_BROWSERS_PATH`, `TEMP`, and `TMP` only for
that process. Production TLS remains verified; primary ingestion state is not
published or overwritten during these checks.

Final targeted results: all five formerly failed SPBE tables passed twice with
byte-identical normalized records (a fixed validation timestamp), matching page
ranges and complete block/slot coverage. SPBE 2024 recovered 41 rows (`PD/IKU`,
three pages), 32 rows (`Perangkat Daerah/Jml`) and 13 rows (`IKU/Jumlah`). SPBE
2025 recovered 39 rows (`Perangkat Daerah/Jlh`) and 13 rows (`IKU/PD`). The latter
four tables each used one page. These are 138 recovered rows in isolated runtime
validation artifacts, not a full ingestion/publication. Twenty-four targeted
Looker tests passed, including block gaps, slot gaps, mixed position bases, equal
data rows, ordinal-free virtual blocks and headerless accessible-table rejection.
Other report components were not extracted during this final validation.
