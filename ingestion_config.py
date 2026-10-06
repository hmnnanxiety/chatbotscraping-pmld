"""Small, process-local ingestion configuration. No client is created here."""
from dataclasses import dataclass
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import tempfile
import warnings

from dotenv import load_dotenv
from urllib3.exceptions import InsecureRequestWarning

ROOT = Path(__file__).resolve().parent
COOKIE_ENV_NAMES = (
    "DWH_COOKIE_APP_SESSION", "DWH_COOKIE_LARAVEL_SESSION",
    "DWH_COOKIE_XSRF_EXTRA", "DWH_COOKIE_XSRF_TOKEN",
)
log = logging.getLogger("etl.config")
_tls_warning_emitted = False

class ConfigurationError(ValueError):
    pass

@lru_cache(maxsize=1)
def load_environment():
    """Load only this project's .env, once. Explicit process variables take precedence."""
    path = ROOT / ".env"
    try:
        exists = path.is_file()
        load_dotenv(path, override=False, encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("Cannot read .env; check file permissions and UTF-8 encoding.") from exc
    return exists

@dataclass(frozen=True)
class Configuration:
    verify: bool | str
    env_found: bool
    cookie_status: str
    app_env: str = "development"

@lru_cache(maxsize=1)
def get_config():
    env_found = load_environment()
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    if app_env not in {"development", "production"}:
        raise ConfigurationError("APP_ENV must be development or production.")
    flag = os.environ.get("IDMC_VERIFY_TLS", "true").strip().lower()
    if app_env == "production" and flag in {"false", "0", "no"}:
        raise ConfigurationError("TLS verification cannot be disabled in production.")
    ca = os.environ.get("IDMC_CA_BUNDLE", "").strip()
    if ca:
        path = Path(ca).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise ConfigurationError(f"IDMC_CA_BUNDLE does not exist or is not a file: {path}")
        try:
            with path.open("rb") as stream:
                stream.read(1)
        except OSError as exc:
            raise ConfigurationError(f"IDMC_CA_BUNDLE is not readable: {path}") from exc
        verify = str(path.resolve())
    else:
        if flag not in {"true", "false", "1", "0", "yes", "no"}:
            raise ConfigurationError("IDMC_VERIFY_TLS must be true or false.")
        verify = flag in {"true", "1", "yes"}
    count = sum(bool(os.environ.get(name, "").strip()) and
                not os.environ[name].strip().startswith("paste-") for name in COOKIE_ENV_NAMES)
    status = "available" if count == len(COOKIE_ENV_NAMES) else "partial" if count else "missing (optional)"
    return Configuration(verify, env_found, status, app_env)

def tls_verify(override=None):
    """Every HTTP constructor calls this, even when imported before the CLI."""
    global _tls_warning_emitted
    config = get_config()
    verify = config.verify if override is None else override
    if verify is False:
        if config.app_env == "production":
            raise ConfigurationError("TLS verification cannot be disabled in production.")
        # Narrow suppression only: no other warning category is silenced.
        warnings.filterwarnings("ignore", category=InsecureRequestWarning)
        if not _tls_warning_emitted:
            log.warning("[config][warn] TLS verification is disabled. Development workaround only.")
            _tls_warning_emitted = True
    return verify

def startup_check(source_config, output_dir, emit=print):
    config = get_config()
    tls_verify()
    path = Path(source_config)
    try:
        source = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Source config does not exist: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Source config cannot be read/parsed as JSON: {path}") from exc
    if not isinstance(source, dict) or not isinstance(source.get("superset", True), bool):
        raise ConfigurationError("Source config must be an object with a boolean superset option.")
    if not isinstance(source.get("cctv_groups", []), list) or not isinstance(source.get("web_sources", []), list):
        raise ConfigurationError("cctv_groups and web_sources must be lists.")
    output = Path(output_dir)
    try:
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=output) as probe:
            probe.write(b"configuration check")
            probe.flush()
    except OSError as exc:
        raise ConfigurationError(f"Output directory is not writable: {output}") from exc
    emit(f"[config] Environment: {config.app_env}")
    emit("[config] Configuration: " + (".env loaded" if config.env_found else "process/defaults (.env not present)"))
    emit(f"[config] Source config: {path}")
    mode = "verification disabled (development mode)" if config.verify is False else "verified" if config.verify is True else "custom CA bundle"
    emit(f"[config] TLS: {mode}")
    if isinstance(config.verify, str):
        emit(f"[config] CA bundle: {config.verify}")
    emit(f"[config] Cookie fallback: {config.cookie_status} (presence only; not validated)")
    emit(f"[config] Output directory: {output} (writable)")
    return config

def friendly_http_error(exc=None, *, status=None, context="request"):
    """Actionable errors without exception text, response bodies, URLs or credentials."""
    import requests
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS/certificate failure. Configure IDMC_CA_BUNDLE with a trusted CA; IDMC_VERIFY_TLS=false is development-only."
    if isinstance(exc, requests.Timeout):
        return "Network timeout. Retry later; check source availability and your connection."
    if isinstance(exc, requests.ConnectionError):
        return "Network connection failed. Check network/proxy settings and source availability."
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = response.status_code
    if status in (401, 403):
        return f"{context} returned HTTP {status}. Portal session/fallback cookies may be missing or expired; refresh values in .env and retry."
    if status == 422:
        return f"{context} returned HTTP 422. Check this dashboard's referer setting in dashboards.py."
    if status == 429:
        return f"{context} returned HTTP 429 (rate limit). Wait and retry."
    if status is not None and status >= 500:
        return f"{context} returned HTTP {status}. Source service is temporarily unavailable; retry later."
    if status is not None:
        return f"{context} returned HTTP {status}. Check source access/configuration."
    return f"{context} failed ({type(exc).__name__ if exc else 'unknown error'}). Check source availability/configuration."
