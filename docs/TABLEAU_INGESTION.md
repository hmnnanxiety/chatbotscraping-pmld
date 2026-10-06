# Tableau Public: Jaringan di DIY / Fiber Optic

The [final standalone full run](FINAL_VALIDATION.md) confirmed one record, three
visible metric rows and one chunk; both detailed worksheets remain unsupported.
The historical targeted evidence below explains the mechanism and its limits.

Standalone backport of the validated experiment implementation. Portal API entry 171 currently maps
`Dashboard Ajimandaya` → `Jaringan di DIY` to workbook `DashboardJaringanDIY`,
view `FO`. Names/visibility still come from the verified portal scope; workbook,
view, worksheet names, metric zone IDs and source URL stay in provenance.

## Verified mechanism and limits

Live inspection on 2026-10-06 found two worksheets, `Sheet 1 (3)` and `Sheet 11`.
Direct CSV routes returned 404. The official Embedding API
`getSummaryDataReaderAsync()` returned HTTP 403 `PermissionDeniedException` for
both worksheets. No alternate data-export command, permission workaround,
underlying-data request, screenshot or OCR is implemented.

The public viewer's ordinary `bootstrapSession` response already contains
visible dashboard text-zone captions. Three are publisher-authored metric cards,
not worksheet summary/query results. The adapter reads only configured labels
and explicit units from visible text zones, then verifies the same captions
are exposed in the initialized viewer's DOM. It discards the rest of the response,
including session identifiers. No full bootstrap/session payload is persisted.

Current published values: `Panjang Kabel Udara` = `585.813 m`,
`Panjang Kabel Tanam` = `37.243 m`, `Panjang Agregat` = `623.056 m`.
These are source display strings: separator/locale interpretation is not guessed.
The viewer reports `Data Source : Kominfo DIY` and
`Last Update : 9/18/2026 10:30:01 AM`. This date is retained verbatim as
`source_update_date`; its timezone/statistical period is not inferred.

Rows: `metric`, `display_value`, `unit`. Output uses the existing schema revision
2.1.0 and deterministic canonical row/chunk ordering. `extraction_mode` is
`visible_metric_cards`, never `metadata_only`. All three configured cards must
be present once, with valid values/units, or extraction fails explicitly.

`is_complete=false` refers to the full view; `metric_cards_complete=true` refers
only to configured published cards. These cards are dashboard text, not assumed
to change with Kabupaten/Kota worksheet filters. `effective_filters=[]` is not
a claim that the underlying map is unfiltered; `filter_binding` documents that
no relationship to worksheet filters is inferred.

Real worksheet identities are discovered via the official active-sheet API and
reported as skipped/unsupported targets. Forcing their extraction also fails
explicitly. Consequently a successful targeted run is **PARTIAL**, with one
aggregate record/three rows and two unsupported worksheet targets. Detailed
routes, geometry, camera data, per-region breakdowns and underlying rows are
not extracted or fabricated. Tableau is supported **only for these visible
publisher-authored cards**, not as a general worksheet-data adapter.

## Browser and operation

An isolated headless Chromium initializes the public view through Tableau's
official Embedding API. No signed-in browser profile or sibling project is used.
The existing optional `requirements-looker.txt` also supplies Playwright here;
no additional package or AI/vector dependency is added. Chromium uses platform
TLS trust (`ignore_https_errors=false`); existing HTTP custom CA rules stay intact.

Bootstrap layout is not a stable public data API. Changed framing/layout,
duplicate/missing cards, hidden cards, view identity changes or network/DOM
disagreement fail closed. Retries, per-target isolation, stale carry-forward,
minimum success guard and atomic publication remain owned by the generic ETL.

Targeted offline tests:

```powershell
python -B -m unittest tests.test_tableau_public -v
```

Targeted live validation, no full ingestion or main runtime publication:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD/runtime/playwright-browsers"
python -B -m tests.validate_tableau_live --output-dir runtime/tableau-targeted
```

The validator restricts discovery to this source, checks current portal mapping,
extracts twice using independent browser sessions with a fixed validation clock,
compares normalized records/chunks, checks row coverage and writes only sanitized
validation output in the selected directory. It does not retry denied data APIs.

Historical experiment live validation: both independent sessions produced identical normalized
records/chunks (one record, three rows, one chunk), with complete metric-row
coverage and unchanged primary runtime state. Both real worksheets were identified
as unsupported. Eighteen Tableau-specific tests and 96 related regression tests
passed; `git diff --check` was clean. Bootstrap framing was verified as UTF-16
code-unit lengths, including Unicode/astral characters. Visible worksheet identity
uses dashboard zones plus official active-dashboard membership: a hidden workbook
tab does not imply that its map is hidden inside the dashboard.

Production snapshots and experiment validation artifacts are not copied into
this standalone project. Automated tests use synthetic fixtures only; no sibling
repository is needed. The live validator above is optional and writes only to an
isolated local runtime subdirectory. No full ingestion is required for validation.
