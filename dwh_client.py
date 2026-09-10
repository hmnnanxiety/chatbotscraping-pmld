"""
dwh_client.py

Reusable client for the Jogja Provincial Government DWH (Apache Superset).

Key fact this client is built around: guest tokens are scoped PER
DASHBOARD (confirmed by inspecting multiple dashboards' network traffic)
— a token issued for one dashboard's UUID does not authorize requests
against another. So this client tracks one token per dashboard UUID,
not a single global token.

Handles: base session cookies, per-dashboard guest tokens (with caching),
rate limiting, retry/backoff, UUID -> numeric dashboard id resolution,
chart metadata, and chart data fetching.

Imported by:
    - dwh_ingest.py       (scheduled job: refreshes chatbot_metadata.db)
    - chart_service.py    (live backend: fetches chart data on demand)
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SUPERSET_DOMAIN = "https://dwh.jogjaprov.go.id"
GUEST_TOKEN_ENDPOINT = "https://idmc.jogjaprov.go.id/backend/api/v1/superset/guest-token"
PORTAL_REFERER = "https://idmc.jogjaprov.go.id/"
REQUEST_TIMEOUT = 15  # seconds

# --- Rate-limiting / retry safety net ---------------------------------------
MIN_REQUEST_INTERVAL = 1.5
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 30.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# --- Guest token cache (one entry per dashboard uuid) ------------------------
TOKEN_CACHE_PATH = ".dwh_token_cache.json"
TOKEN_CACHE_TTL_SECONDS = 180

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

REQUIRED_COOKIE_ENV_MAP = {
    "5DVXMhconVKQyySGVJLzKYo3u2hMOPAl3EuoI02g": "DWH_COOKIE_APP_SESSION",
    "laravel_session": "DWH_COOKIE_LARAVEL_SESSION",
    "vft9zsKnsM0VUKp27CsoVDPhve4CY8mscGEq2Xwi": "DWH_COOKIE_XSRF_EXTRA",
    "XSRF-TOKEN": "DWH_COOKIE_XSRF_TOKEN",
}


# --------------------------------------------------------------------------- #
# Startup / config check
# --------------------------------------------------------------------------- #

def check_environment() -> bool:
    print("-" * 60)
    print("Startup check")
    print("-" * 60)

    missing = [
        env_var
        for env_var in REQUIRED_COOKIE_ENV_MAP.values()
        if not os.environ.get(env_var)
    ]

    if missing:
        print("Status : FAILED")
        print(f"Reason : missing environment variable(s): {', '.join(missing)}")
        print()
        print("These are used as a fallback if the portal does not issue")
        print("session cookies automatically. To fix this:")
        print("  1. Log in to https://idmc.jogjaprov.go.id/ in your browser")
        print("  2. Open DevTools > Application > Cookies")
        print("  3. Copy the current cookie values into your .env file")
        print("  4. Re-run this script")
        print("-" * 60)
        return False

    print("Status : OK")
    print("Detail : all required fallback cookie variables are set")
    print("-" * 60)
    return True


# --------------------------------------------------------------------------- #
# Rate limiting + retry
# --------------------------------------------------------------------------- #

class RateLimiter:
    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL) -> None:
        self.min_interval = min_interval
        self._last_request_at: Optional[float] = None

    def wait(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()


def _request_with_backoff(request_fn, *, max_retries: int = MAX_RETRIES, context: str = "request"):
    last_exc: Optional[Exception] = None
    last_response: Optional[requests.Response] = None

    for attempt in range(1, max_retries + 1):
        try:
            response = request_fn()
        except requests.exceptions.RequestException as e:
            last_exc = e
            print(f"[retry] {context}: attempt {attempt}/{max_retries} failed ({e})")
        else:
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            last_response = response
            print(
                f"[retry] {context}: attempt {attempt}/{max_retries} got "
                f"HTTP {response.status_code} — treating as transient"
            )

        if attempt < max_retries:
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            delay += random.uniform(0, 0.5)
            print(f"[retry] {context}: backing off {delay:.1f}s before next attempt")
            time.sleep(delay)

    if last_response is not None:
        return last_response
    raise last_exc  # type: ignore[misc]


def _load_token_cache() -> Dict[str, Dict[str, Any]]:
    """Load the full {uuid: {token, cached_at}} cache dict from disk."""
    if not os.path.exists(TOKEN_CACHE_PATH):
        return {}
    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _get_cached_token(dashboard_uuid: str) -> Optional[str]:
    cache = _load_token_cache()
    entry = cache.get(dashboard_uuid)
    if not entry:
        return None
    age = time.time() - entry.get("cached_at", 0)
    if age < TOKEN_CACHE_TTL_SECONDS:
        return entry.get("token")
    return None


def _save_cached_token(dashboard_uuid: str, token: str) -> None:
    cache = _load_token_cache()
    cache[dashboard_uuid] = {"token": token, "cached_at": time.time()}
    try:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError as e:
        print(f"[auth][warn] Could not write token cache: {e}")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class DWHClient:
    """
    One requests.Session shared across all dashboards (base cookies are
    portal-wide), but one guest token PER dashboard uuid — each request
    explicitly sets the Authorization/X-GuestToken headers for the
    dashboard it's about to talk to, rather than relying on a single
    session-wide token.
    """

    def __init__(
        self,
        known_dashboard_ids: Optional[Dict[str, int]] = None,
        known_referers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "Referer": PORTAL_REFERER,
            }
        )
        self._rate_limiter = RateLimiter()
        self._tokens: Dict[str, str] = {}            # dashboard_uuid -> token
        self._referers: Dict[str, str] = known_referers or {}  # dashboard_uuid -> specific referer
        # Pre-seed with known uuid -> numeric_id mappings so no resolution
        # attempt is made for dashboards whose id is already known.
        self._dashboard_info: Dict[str, Dict[str, Any]] = {
            uuid: {"id": numeric_id, "title": ""}
            for uuid, numeric_id in (known_dashboard_ids or {}).items()
        }

    # --- low-level transport ------------------------------------------- #

    def _get(self, url: str, *, context: str, **kwargs) -> requests.Response:
        def do_request() -> requests.Response:
            self._rate_limiter.wait()
            return self.session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)

        return _request_with_backoff(do_request, context=context)

    def load_base_cookies(self) -> None:
        """Establish portal-wide base session cookies (shared by all dashboards)."""
        print("[auth] Bootstrapping session cookies from portal")
        try:
            self._get(PORTAL_REFERER, context="bootstrap")
        except requests.exceptions.RequestException as e:
            print(f"[auth][warn] Bootstrap request failed: {e}")

        missing = [
            cookie_name
            for cookie_name in REQUIRED_COOKIE_ENV_MAP
            if not self.session.cookies.get(cookie_name)
        ]

        if missing:
            print(f"[auth] Bootstrap didn't set: {missing} — trying .env fallback")
            for cookie_name in missing:
                env_var = REQUIRED_COOKIE_ENV_MAP[cookie_name]
                value = os.environ.get(env_var)
                if value:
                    self.session.cookies.set(cookie_name, value, domain="idmc.jogjaprov.go.id")
                else:
                    print(f"[auth][warn] No fallback for {cookie_name} ({env_var} not set)")
        else:
            print("[auth] All base cookies acquired automatically")

        xsrf_cookie = self.session.cookies.get("XSRF-TOKEN")
        if xsrf_cookie:
            self.session.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf_cookie)

    # --- per-dashboard guest tokens -------------------------------------- #

    def get_guest_token(self, dashboard_uuid: str, force_refresh: bool = False) -> Optional[str]:
        """Fetch (or reuse a cached) guest token scoped to one dashboard."""
        if not force_refresh and dashboard_uuid in self._tokens:
            return self._tokens[dashboard_uuid]

        if not force_refresh:
            cached = _get_cached_token(dashboard_uuid)
            if cached:
                print(f"[auth] Reusing cached guest token for {dashboard_uuid[:8]}...")
                self._tokens[dashboard_uuid] = cached
                return cached

        print(f"[auth] Requesting guest token for {dashboard_uuid[:8]}...")
        url = (
            f"{GUEST_TOKEN_ENDPOINT}?dashboardId={dashboard_uuid}"
            f"&supersetDomain={urllib.parse.quote(SUPERSET_DOMAIN, safe='')}"
        )
        referer = self._referers.get(dashboard_uuid, PORTAL_REFERER)
        try:
            response = self._get(
                url,
                context=f"guest-token:{dashboard_uuid[:8]}",
                headers={"Referer": referer},
            )
        except requests.exceptions.RequestException as e:
            print(f"[auth] Guest token request failed: {e}")
            return None

        if response.status_code == 422:
            print("[auth] Guest token request rejected (422 Unprocessable).")
            print("[auth] This dashboard likely requires a specific Referer header")
            print("[auth] (parent-menu-slug/child-menu-slug/base64(urlencode(source))).")
            print("[auth] Add it to this dashboard's 'referer' field in dashboards.py.")
            return None

        if response.status_code in (401, 403):
            print(f"[auth] Guest token request rejected ({response.status_code}).")
            print("[auth] Your session cookies have likely expired — refresh .env.")
            return None

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"[auth] Guest token request failed: {e}")
            return None

        token = response.json().get("token")
        if not token:
            print(f"[auth] Response OK but no token present: {response.json()}")
            return None

        print(f"[auth] Guest token acquired for {dashboard_uuid[:8]}...")
        self._tokens[dashboard_uuid] = token
        _save_cached_token(dashboard_uuid, token)
        return token

    def _authed_get(self, url: str, dashboard_uuid: str, *, context: str, **kwargs) -> requests.Response:
        """GET with the Authorization/X-GuestToken headers for one specific dashboard."""
        token = self.get_guest_token(dashboard_uuid)
        if not token:
            raise RuntimeError(f"Could not authenticate for dashboard {dashboard_uuid}.")

        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GuestToken"] = token

        response = self._get(url, context=context, headers=headers, **kwargs)

        if response.status_code in (401, 403):
            print(f"[auth] Got {response.status_code} for {dashboard_uuid[:8]}, refreshing token...")
            token = self.get_guest_token(dashboard_uuid, force_refresh=True)
            if not token:
                raise RuntimeError(f"Re-authentication failed for dashboard {dashboard_uuid}.")
            headers["Authorization"] = f"Bearer {token}"
            headers["X-GuestToken"] = token
            response = self._get(url, context=f"{context}-retry", headers=headers, **kwargs)

        response.raise_for_status()
        return response

    # --- dashboard resolution -------------------------------------------- #

    def get_dashboard_info(self, dashboard_uuid: str) -> Dict[str, Any]:
        """
        Return a dashboard's numeric id (and title, if known).

        There is no API call that resolves UUID -> numeric id — Superset's
        /api/v1/dashboard/{id} endpoint only accepts the numeric id, and
        the numeric id is only visible inside the embedded viewer's
        server-rendered HTML bootstrap data. So this only returns
        pre-seeded ids (from known_dashboard_ids at construction, i.e.
        DASHBOARDS in dashboards.py) — it does not attempt to fetch or
        guess one.
        """
        if dashboard_uuid in self._dashboard_info:
            return self._dashboard_info[dashboard_uuid]

        raise RuntimeError(
            f"No known numeric id for dashboard {dashboard_uuid}. "
            "Find it manually (DevTools > Network > filter 'embedded' > "
            "inspect the HTML response for a 'dashboard_title'/'id' pair) "
            "and add it to DASHBOARDS in dashboards.py."
        )

    # --- chart metadata + data -------------------------------------------- #

    def get_dashboard_charts(self, dashboard_uuid: str) -> List[Dict[str, Any]]:
        """Fetch {slice_id, slice_name} pairs for one dashboard."""
        info = self.get_dashboard_info(dashboard_uuid)
        numeric_id = info["id"]

        url = f"{SUPERSET_DOMAIN}/api/v1/dashboard/{numeric_id}/charts"
        response = self._authed_get(url, dashboard_uuid, context="charts")
        payload = response.json()
        raw_charts = payload.get("result", payload) if isinstance(payload, dict) else payload

        extracted: List[Dict[str, Any]] = []
        for chart in raw_charts:
            slice_id = chart.get("id")
            slice_name = chart.get("slice_name")
            if slice_id is None or slice_name is None:
                continue
            extracted.append({"slice_id": slice_id, "slice_name": slice_name})

        return extracted

    def get_chart_data(self, dashboard_uuid: str, slice_id: int) -> Dict[str, Any]:
        """
        Fetch actual data rows for one chart.

        Uses the confirmed frontend pattern (captured from real browser
        traffic): a GET to /api/v1/chart/data with form_data + dashboard_id
        as query params, scoped to that dashboard's numeric id and its
        own guest token.
        """
        info = self.get_dashboard_info(dashboard_uuid)
        numeric_id = info["id"]

        form_data = json.dumps({"slice_id": slice_id})
        url = (
            f"{SUPERSET_DOMAIN}/api/v1/chart/data"
            f"?form_data={urllib.parse.quote(form_data)}"
            f"&dashboard_id={numeric_id}"
        )

        print(f"[chart] Fetching data for slice_id={slice_id} (dashboard {numeric_id})")
        response = self._authed_get(url, dashboard_uuid, context="chart-data")
        payload = response.json()

        results = payload.get("result", [])
        if not results:
            raise RuntimeError(f"Chart {slice_id}: data response had no 'result' entries.")

        first = results[0]
        rows = first.get("data", [])
        colnames = first.get("colnames", [])

        print(f"[chart] Retrieved {len(rows)} row(s) for slice_id={slice_id}")
        return {
            "slice_id": slice_id,
            "dashboard_uuid": dashboard_uuid,
            "colnames": colnames,
            "rows": rows,
        }
