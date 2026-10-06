# scraping-pipeline

Standalone ingestion for the IDMC DIY portal: source discovery → extraction →
normalization → validation → delta/change detection → staging → whole-row
chunking → JSON output. No sibling repository is imported or required.

**The scope stops before embedding, vector databases, retrieval, RAG and chat.**
There is no FastAPI, Gemini, Chroma, frontend application or answer generation.
`ready_for_vector_db_v2.json` is a compatibility filename, not a vector operation.

## Setup and run (PowerShell)

Python 3.12 is the pinned dependency target; final live validation also passed
on Python 3.13. Install the optional browser set for Looker and Tableau:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-looker.txt
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD/runtime/playwright-browsers"
python -m playwright install chromium --only-shell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Configure .env for your environment before running:
python -B -m tests.smoke_config --output-dir runtime
python -B main_orchestrator.py --output-dir runtime
```

Use the same browser-cache variable when running in a new shell. Base HTTP
sources and synthetic tests do not need Chromium; missing browser dependencies
fail browser-source extraction explicitly. Browser DOM tests may skip without an
installed browser. `tests.smoke_config` is offline and does not validate cookies.

## Production TLS and configuration

The supplied `.env.example` explicitly selects **development with TLS disabled**.
It is not a production template as-is. Unset TLS configuration defaults to verified
HTTPS. For production, configure local `.env` or process variables:

```dotenv
APP_ENV=production
IDMC_VERIFY_TLS=true
IDMC_CA_BUNDLE=certs/idmc-ca-bundle.pem
```

Obtain a trusted PEM CA bundle through your deployment administrator; the path
must be readable (relative paths resolve inside this project). Omit the CA setting
when standard trust suffices. Production rejects `IDMC_VERIFY_TLS=false`, even
with a CA configured. Development-only insecure HTTP emits a warning. Chromium
uses platform trust, not the HTTP PEM setting, and never bypasses HTTPS errors.

`.env` loads once; process variables take precedence. Restart after changes.
The four `DWH_COOKIE_*` values are optional Superset fallback credentials; refresh
expired cookies locally, never commit or log them. Source inventory lives in
`ingestion_sources.json` and `dashboards.py`; current visibility comes from the
portal APIs, not a hardcoded dashboard count.

## Supported extraction

| Source | Extraction boundary |
| --- | --- |
| Superset | Guest-token/session transport, dashboard/chart discovery, stored queries with effective default filters |
| CCTV native | Configured location filters, complete next-link pagination and unique-ID/total checks |
| Looker Studio | Rendered structured tables on configured report pages; pagination and virtual-row coverage |
| Tableau Public | Fiber Optic's three published metric cards; detailed worksheet export is unavailable |
| Grafana/xPlore | Supported saved public-panel queries and typed DataFrames; custom renderers/transforms explicitly unsupported |

Portal names and internal source IDs/names are both retained. Hidden sources and
their previous records cannot enter published chunks. Perkebunan remains an
explicit legacy **metadata-only** record, not extracted table data.

## Outputs and reliability

Always pass `--output-dir runtime` (or a separate validation directory); the CLI
default is the repository root. Outputs are normalized staging, chunk JSON,
delta state, extraction report and diagnostic log. Schema container version is
`2`; normalization/transform revision is `2.1.0`.

Rows are canonicalized and never split. IDs/hashes/grouping are deterministic for
identical input/configuration. Oversized individual rows are flagged, not truncated.
Retries and per-target isolation preserve healthy sources; stale carry-forward
never advances the last successful-fetch timestamp. Validated output publication
has locking, atomic file replacement, rollback/recovery and delta committed last.
Exit codes: `0` ok, `2` usable partial, `1` aborted with known-good data preserved.

## Offline verification

```powershell
python -B -m unittest discover -s tests -v
python -B -m tests.reproduce --output-dir runtime/reproduction
python -B -m tests.reproduce_frontend --output-dir runtime/frontend-reproduction
python -B transformer.py --staging runtime/staging_idmc_data.json --output runtime/ready_for_vector_db_v2.json
```

Both reproduction commands publish twice from small synthetic fixtures and check
byte-identical staging/chunks/delta plus exact row coverage. No production snapshot,
credentials or live service is a test prerequisite.

## Handoff evidence and documentation

Final live validation (2026-10-06): **9 active menus, 25 mapped sources, 0 unmapped,
7 hidden excluded; 97 records, 5,550 rows, 2,436 chunks; 183 tests passed**.
Status **PARTIAL**: UMKM timeouts, overlapping CCTV SPL/Kota page IDs, Perkebunan
metadata-only, non-table Looker visuals, denied Tableau details and unsupported
Grafana panels. Mapping coverage is not complete underlying-data coverage.

- [Architecture, contracts, configuration and operations](docs/INGESTION_V2.md)
- [Source discovery/mapping walkthrough and debugging findings](docs/SOURCE_DISCOVERY_WALKTHROUGH.md)
- [Final live validation, integrity checks and known limitations](docs/FINAL_VALIDATION.md)
- Adapter details: [Looker](docs/LOOKER_INGESTION.md), [Tableau](docs/TABLEAU_INGESTION.md), [Grafana](docs/GRAFANA_INGESTION.md)
