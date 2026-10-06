"""Adapter protocol, transport and retry policy. Retries never include permanent 4xx."""
from __future__ import annotations

import logging
import time
from typing import Protocol
from urllib.parse import urlparse

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from contracts import SourceTarget, UnifiedRecord
from ingestion_config import tls_verify, friendly_http_error

log = logging.getLogger("etl.retry")


def transient_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    if getattr(exc, "ingestion_category", None) == "timeout":
        # Browser adapters expose timeouts without depending on requests exceptions.
        return True
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in {408, 429, 500, 502, 503, 504}


def _log_retry(state):
    log.warning("Retrying source after attempt %d: %s", state.attempt_number,
                friendly_http_error(state.outcome.exception()))


retry_source = retry(
    retry=retry_if_exception(transient_error),
    stop=stop_after_attempt(3),
    wait=wait_random_exponential(multiplier=1, max=20),
    before_sleep=_log_retry, reraise=True,
)


def origin(url: str) -> tuple:
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("Only HTTPS URLs without embedded credentials are supported")
    return p.scheme, p.hostname.lower(), p.port or 443


class HTTPClient:
    def __init__(self, session=None, timeout=30, min_interval=1.5, verify=None):
        self.session = session or requests.Session()
        self.timeout, self.min_interval = timeout, min_interval
        self.verify = tls_verify(verify)
        self.session.verify = self.verify
        self._last = 0.0

    def get(self, url: str, *, allowed_origins=None):
        allowed = set(allowed_origins or {origin(url)})
        # Validate redirects too; a same-origin next link must not redirect off-site.
        for _ in range(6):
            if origin(url) not in allowed:
                raise ValueError("Cross-origin request/redirect rejected")
            delay = self.min_interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()
            response = self.session.get(
                url, timeout=self.timeout, verify=self.verify, allow_redirects=False,
                headers={"User-Agent": "IDMC-Snapshot-Ingestion/2.0", "Accept": "*/*"},
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                from urllib.parse import urljoin
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Redirect has no Location")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            return response
        raise ValueError("Too many redirects")


class SourceAdapter(Protocol):
    source_type: str

    def discover(self) -> list[SourceTarget]: ...

    def extract(self, target: SourceTarget) -> list[UnifiedRecord]: ...
