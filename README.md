# chatbotscraping-pmld

A client + ingestion pipeline for pulling live chart data out of the Jogja
Provincial Government's **IDMC data warehouse portal**
([idmc.jogjaprov.go.id](https://idmc.jogjaprov.go.id/)), which embeds an
Apache Superset instance (`dwh.jogjaprov.go.id`) to power its public
dashboards.

Instead of scraping rendered HTML, this project reverse-engineers and talks
directly to Superset's underlying `/api/v1/chart/data` JSON API, using the
same per-dashboard guest-token auth flow the embedded frontend itself uses.

## How it works

1. **Auth**: the portal issues Laravel session cookies. Those are exchanged,
   per dashboard, for a short-lived Superset **guest token** (JWT) via
   `idmc.jogjaprov.go.id/backend/api/v1/superset/guest-token`. Guest tokens
   are scoped to a single dashboard UUID — a token for one dashboard will
   not authorize requests against another.
2. **Dashboard registry** (`dashboards.py`): every known Superset-backed
   dashboard, its UUID (from the portal menu), and its numeric Superset
   dashboard ID. Superset's chart/dashboard endpoints only accept the
   numeric ID, and there's currently no API that resolves UUID → numeric ID
   — it has to be found manually per dashboard by inspecting the embedded
   viewer's bootstrap HTML. Dashboards with `numeric_id: None` are skipped
   until that's done.
3. **Client** (`dwh_client.py`): `DWHClient` — session/cookie bootstrap,
   guest-token fetch + on-disk caching + refresh-on-401, a rate limiter with
   backoff/retry, and chart metadata + data fetching.
4. **Ingestion** (`dwh_ingest.py`): a scheduled job that walks every known
   dashboard, pulls its chart catalog, and upserts it into a local SQLite
   `charts_metadata` table (`chatbot_metadata.db`) so the catalog stays
   fresh without hitting Superset on every query.
5. **Service layer** (`chart_service.py`): thin wrapper used by the
   chatbot/backend — `list_charts()` reads the SQLite catalog,
   `get_chart_data(slice_id)` resolves which dashboard a chart belongs to
   and fetches its live data with that dashboard's own guest token.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Then fill in `.env` with real cookie values:

1. Log in to https://idmc.jogjaprov.go.id/ in your browser.
2. Open DevTools → Application → Cookies.
3. Copy the current cookie values into `.env`.

These act as a fallback in case the portal doesn't set session cookies
automatically when the client bootstraps its own session.

## Usage

Refresh the chart catalog:

```bash
python dwh_ingest.py
```

Fetch live data for a specific chart in code:

```python
from chart_service import list_charts, get_chart_data

charts = list_charts()          # [{"slice_id", "slice_name", "dashboard_name"}, ...]
data = get_chart_data(608)      # {"slice_id", "dashboard_uuid", "colnames", "rows"}
```

## Adding a new dashboard

1. Find its UUID from the portal's menu.
2. Open the embedded viewer at `https://dwh.jogjaprov.go.id/embedded/<uuid>`,
   open DevTools → Network, filter for `embedded`, and inspect the raw HTML
   response for its numeric `dashboard_title`/`id` pair.
3. Add an entry to `DASHBOARDS` in `dashboards.py`. If the guest-token
   request 422s, the dashboard needs a specific `Referer` header — capture
   it from real browser traffic and set it in that dashboard's `referer`
   field.

## Security notes

- `.env`, `.dwh_token_cache.json`, and `chatbot_metadata.db` are all
  git-ignored — they hold live session cookies, live guest tokens, and a
  regenerable data cache respectively, and none of them belong in version
  control.
- `dwh_client.py` disables TLS certificate verification (`verify=False`)
  to reach the token endpoint. This was necessary during development but
  is worth revisiting before treating this as production-hardened.

## Status / roadmap

- Working: guest-token auth, chart metadata + data fetching, SQLite-backed
  ingestion for 10 of ~11 known Superset dashboards.
- Pending: numeric ID for the CCTV/Surveillance dashboard.
- Planned: FastAPI backend as the client-facing API, plus an LLM
  function-calling layer to turn natural-language queries into structured
  chart-data requests.
