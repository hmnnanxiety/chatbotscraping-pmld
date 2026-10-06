# scraping-pipeline

Standalone IDMC DIY scraping, normalization, staging and whole-row chunking.
The pipeline ends at JSON output. All source code and configuration live here;
no sibling project, production snapshot, local database or external application
is required to run the automated tests.

## Quick Start (PowerShell)

Python 3.12; requirements pin the ingestion dependencies and their transitive dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# Only when .env does not already exist:
Copy-Item .env.example .env
# Fill .env with valid local values, then:
python -m tests.smoke_config
python main_orchestrator.py --output-dir runtime
```

The diagnostic is offline: it checks configuration, dependency imports and output
permissions, not cookie validity. `.env` is loaded once from this project;
existing process environment variables take precedence. Restart after changes.

TLS modes: recommended `IDMC_VERIFY_TLS=true`; for a trusted custom CA use
`IDMC_CA_BUNDLE=C:/path/to/ca.pem` (takes precedence). `IDMC_VERIFY_TLS=false`
is a **development-only workaround**, not a production setting; it emits one
application warning instead of repeated insecure-request warnings.
Fallback cookies can expire: refresh `.env` values on HTTP 401/403.
Never commit `.env` or share cookie/token values in logs.

## Environment mode

`APP_ENV` defaults to `development`; TLS verification defaults to `true`.
Local development may explicitly disable verification:

```dotenv
APP_ENV=development
IDMC_VERIFY_TLS=false
```

Production requires secure TLS:

```dotenv
APP_ENV=production
IDMC_VERIFY_TLS=true
# Optional trusted deployment CA:
# IDMC_CA_BUNDLE=C:/trusted/ca.pem
```

A readable custom CA bundle is the supported production fallback and takes
precedence over normal verification. Production rejects explicit
`IDMC_VERIFY_TLS=false` **even with a CA configured**, before discovery.
Only insecure development suppresses repeated certificate warnings; one application
warning remains. Restart after changing configuration; never commit real `.env`.

## Offline verification

```powershell
python -B -m unittest discover -s tests -v
python -B -m tests.reproduce --output-dir .ingestion-demo
```

Reproduction executes the pipeline twice using a small synthetic source fixture
defined in Python (2 records, 151 rows), compares staging/chunk/delta bytes and
validates every row. No production data or credentials are test fixtures.

## Live ingestion and offline transformation

```powershell
.\.venv\Scripts\python.exe -B main_orchestrator.py --output-dir runtime
.\.venv\Scripts\python.exe -B transformer.py --staging runtime/staging_idmc_data.json --output runtime/ready_for_vector_db_v2.json
```

Sources: `ingestion_sources.json` and `dashboards.py`. Optional local cookie
fallbacks and TLS settings: copy `.env.example` to `.env` and configure locally.
No real credentials are included or read from a sibling project.

Output files:
- `staging_idmc_data.json`: schema v2 normalized records.
- `ready_for_vector_db_v2.json`: plain chunk JSON; historical filename retained
  for downstream contract compatibility, not a database integration.
- `delta_state_v2.json`: independent content/metadata hashes.
- `extraction_report_v2.json`: run outcomes and classified failures.
- `ingestion_errors.log`: local diagnostic log.

Normalization and transform revision remain **2.1.0**. Exit codes:
0 = ok, 2 = usable partial result, 1 = aborted without replacing known-good state.

See [the ingestion contract and operating notes](docs/INGESTION_V2.md).

## Files

- `extractors/`: SourceAdapter protocol, retry/HTTP transport, Superset,
  native CCTV, Tableau/Looker and source registry.
- `contracts.py`, `extractor.py`, `delta_checker.py`, `etl_common.py`:
  generic contracts, reliability, change detection and publication.
- `transformers/`: staging migration, whole-row chunking and validation.
- `main_orchestrator.py`, `transformer.py`: orchestration and offline CLI.
- `dwh_client.py`: source-specific session/token transport.
- `tests/`: offline synthetic tests and deterministic reproduction.

No Git repository/history or runtime datasets were copied into this handoff.
