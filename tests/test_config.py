import contextlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings

import requests
from requests.adapters import BaseAdapter
from urllib3.exceptions import InsecureRequestWarning

# Import clients before configuration is loaded: construction must still be safe.
from dwh_client import DWHClient, PORTAL_REFERER, GUEST_TOKEN_ENDPOINT, SUPERSET_DOMAIN
from extractors.base import HTTPClient
import ingestion_config as config

class CaptureTransport(BaseAdapter):
    def __init__(self):
        self.calls = []
    def send(self, request, **kwargs):
        self.calls.append((request.method, request.url, kwargs.get("verify")))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response._content = json.dumps({"token": "synthetic-token", "data": []}).encode()
        response.headers["Content-Type"] = "application/json"
        return response
    def close(self):
        pass

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(config, "ROOT", self.root))
        self.stack.enter_context(patch.object(config, "_tls_warning_emitted", False))
        self.stack.enter_context(warnings.catch_warnings())
        warnings.simplefilter("always")
        config.get_config.cache_clear()
        config.load_environment.cache_clear()
        self.source = self.root / "sources.json"
        self.source.write_text('{"superset": true}', encoding="utf-8")
    def tearDown(self):
        config.get_config.cache_clear()
        config.load_environment.cache_clear()
        self.stack.close()
        self.tmp.cleanup()

    def env(self, text):
        (self.root / ".env").write_text(text, encoding="utf-8")

    def boundary(self, expected):
        transport = CaptureTransport()
        dwh = DWHClient(request_retries=1)
        dwh._rate_limiter.min_interval = 0
        dwh.session.mount("https://", transport)
        dwh.load_base_cookies()
        with patch("dwh_client._save_cached_token"):
            dwh.get_guest_token("uuid", force_refresh=True)
        dwh._authed_get(SUPERSET_DOMAIN + "/api/v1/dashboard/14", "uuid", context="test")
        dwh._authed_post(SUPERSET_DOMAIN + "/api/v1/chart/data", "uuid", context="test", json={})
        generic = HTTPClient(min_interval=0)
        generic.session.mount("https://", transport)
        generic.get(PORTAL_REFERER + "backend/api/v1/cctv")
        self.assertEqual(dwh.session.verify, expected)
        self.assertEqual(generic.session.verify, expected)
        self.assertEqual(len(transport.calls), 5)
        self.assertTrue(any("guest-token" in url for _, url, _ in transport.calls))
        self.assertTrue(any("dwh.jogjaprov.go.id" in url for _, url, _ in transport.calls))
        self.assertTrue(all(verify == expected for _, _, verify in transport.calls))
        return transport.calls

    def test_false_from_env_reaches_real_sessions_and_send_boundary(self):
        self.env("APP_ENV=development\nIDMC_VERIFY_TLS=false\n")
        # Requests' unrelated environment setting must not override explicit verify=False.
        os.environ["REQUESTS_CA_BUNDLE"] = "unused-untrusted-path"
        with self.assertLogs("etl.config", level="WARNING") as captured:
            self.boundary(False)
        self.assertEqual(len(captured.output), 1)

    def test_true_reaches_every_http_boundary(self):
        self.env("APP_ENV=development\nIDMC_VERIFY_TLS=true\n")
        self.boundary(True)

    def test_custom_ca_takes_priority_and_reaches_every_boundary(self):
        ca = self.root / "trusted.pem"
        ca.write_text("synthetic file; transport mocked", encoding="utf-8")
        self.env(f"APP_ENV=development\nIDMC_VERIFY_TLS=false\nIDMC_CA_BUNDLE={ca.as_posix()}\n")
        self.boundary(str(ca.resolve()))
        self.assertFalse(config._tls_warning_emitted)

    def test_default_secure_without_env(self):
        self.boundary(True)
        self.assertEqual(config.get_config().app_env, "development")

    def test_production_verified_at_all_http_boundaries(self):
        self.env("APP_ENV=production\nIDMC_VERIFY_TLS=true\n")
        self.boundary(True)
        self.assertFalse(config._tls_warning_emitted)

    def test_production_custom_ca_at_all_http_boundaries(self):
        ca = self.root / "trusted.pem"
        ca.write_text("synthetic CA; network transport mocked", encoding="utf-8")
        self.env(f"APP_ENV=production\nIDMC_CA_BUNDLE={ca.as_posix()}\n")
        self.boundary(str(ca.resolve()))
        self.assertFalse(config._tls_warning_emitted)

    def test_production_false_aborts_before_discovery_or_network_even_with_ca(self):
        from main_orchestrator import run
        ca = self.root / "trusted.pem"
        ca.write_text("synthetic", encoding="utf-8")
        for flag in ("false", "0", "no"):
            for bundle in ("", str(ca)):
                with self.subTest(flag=flag, bundle=bool(bundle)):
                    config.get_config.cache_clear()
                    with patch.dict(os.environ, APP_ENV="production", IDMC_VERIFY_TLS=flag, IDMC_CA_BUNDLE=bundle), \
                         patch("extractors.registry.configured_adapters") as discover, \
                         patch("requests.sessions.Session.send") as send, \
                         contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(run(config_path=self.source, output_dir=self.root / "out"), 1)
                        discover.assert_not_called()
                        send.assert_not_called()
                        self.assertIn("TLS verification cannot be disabled in production", output.getvalue())
                        self.assertNotIn("Starting extraction", output.getvalue())
                        self.assertFalse((self.root / "out").exists())

    def test_production_client_override_cannot_disable_tls(self):
        self.env("APP_ENV=production\nIDMC_VERIFY_TLS=true\n")
        with patch("requests.sessions.Session.send") as send:
            for client in (DWHClient, HTTPClient):
                with self.assertRaisesRegex(config.ConfigurationError, "cannot be disabled in production"):
                    client(verify=False)
            send.assert_not_called()

    def test_production_does_not_suppress_insecure_warning(self):
        self.env("APP_ENV=production\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config.tls_verify()
            warnings.warn("certificate", InsecureRequestWarning)
        self.assertEqual([w.category for w in caught], [InsecureRequestWarning])

    def test_invalid_environment_fails_without_echoing_value(self):
        self.env("APP_ENV=SECRET_INVALID_MODE\n")
        with self.assertRaisesRegex(config.ConfigurationError, "APP_ENV must be") as caught:
            config.get_config()
        self.assertNotIn("SECRET_INVALID_MODE", str(caught.exception))

    def test_production_ca_must_exist_and_be_readable(self):
        self.env("APP_ENV=production\nIDMC_CA_BUNDLE=missing.pem\n")
        with self.assertRaisesRegex(config.ConfigurationError, "does not exist"):
            config.get_config()
        ca = self.root / "trusted.pem"
        ca.write_text("synthetic", encoding="utf-8")
        os.environ["IDMC_CA_BUNDLE"] = str(ca)
        with patch.object(Path, "open", side_effect=PermissionError("private detail")):
            with self.assertRaisesRegex(config.ConfigurationError, "not readable") as caught:
                config.get_config()
        self.assertNotIn("private detail", str(caught.exception))

    def test_production_startup_summary_has_mode_without_secrets(self):
        self.env("APP_ENV=production\nDWH_COOKIE_APP_SESSION=PRIVATE_COOKIE\n")
        messages = []
        config.startup_check(self.source, self.root / "out", emit=messages.append)
        output = "\n".join(messages)
        self.assertIn("Environment: production", output)
        self.assertIn("TLS: verified", output)
        self.assertNotIn("PRIVATE_COOKIE", output)

    def test_env_loaded_once_before_clients_even_in_reverse_import_order(self):
        self.env("IDMC_VERIFY_TLS=false\n")
        with patch.object(config, "load_dotenv", wraps=config.load_dotenv) as loader:
            self.assertFalse(HTTPClient().verify)
            self.assertFalse(DWHClient().session.verify)
            self.env("IDMC_VERIFY_TLS=true\n")
            self.assertFalse(DWHClient().session.verify)
            self.assertEqual(loader.call_count, 1)

    def test_process_environment_takes_precedence(self):
        self.env("IDMC_VERIFY_TLS=false\n")
        os.environ["IDMC_VERIFY_TLS"] = "true"
        self.assertTrue(config.get_config().verify)

    def test_only_insecure_warning_suppressed_when_disabled(self):
        self.env("IDMC_VERIFY_TLS=false\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config.tls_verify()
            warnings.warn("test certificate", InsecureRequestWarning)
            warnings.warn("keep unrelated warning", UserWarning)
        self.assertEqual([w.category for w in caught], [UserWarning])

    def test_secure_mode_does_not_suppress_insecure_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config.tls_verify()
            warnings.warn("test certificate", InsecureRequestWarning)
        self.assertEqual([w.category for w in caught], [InsecureRequestWarning])

    def test_invalid_flag_and_missing_ca_have_friendly_errors(self):
        self.env("IDMC_VERIFY_TLS=mistyped-secret\n")
        with self.assertRaisesRegex(config.ConfigurationError, "must be true or false") as caught:
            config.get_config()
        self.assertNotIn("mistyped-secret", str(caught.exception))
        os.environ["IDMC_CA_BUNDLE"] = str(self.root / "missing.pem")
        with self.assertRaisesRegex(config.ConfigurationError, "does not exist"):
            config.get_config()

    def test_diagnostics_and_errors_never_print_secrets(self):
        secret = "PRIVATE_COOKIE_VALUE_DO_NOT_LOG"
        self.env("IDMC_VERIFY_TLS=false\n" + "\n".join(name + "=" + secret for name in config.COOKIE_ENV_NAMES))
        messages = []
        with self.assertLogs("etl.config", level="WARNING") as captured:
            config.startup_check(self.source, self.root / "out", emit=messages.append)
        errors = [requests.Timeout(secret), requests.ConnectionError(secret), requests.exceptions.SSLError(secret)]
        for code in (401, 403, 422, 429, 503):
            response = requests.Response()
            response.status_code = code
            response._content = secret.encode()
            errors.append(requests.HTTPError(secret, response=response))
        messages += [config.friendly_http_error(error, context="Guest-token request") for error in errors]
        output = "\n".join(messages + captured.output)
        self.assertNotIn(secret, output)
        self.assertIn("available", output)
        self.assertIn("HTTP 422", output)
        self.assertIn("referer", output)
        self.assertIn("TLS/certificate", output)

    def test_auth_rejection_log_does_not_dump_body_or_exception(self):
        self.env("IDMC_VERIFY_TLS=true\n")
        client = DWHClient(request_retries=1)
        for code in (401, 403, 422):
            response = requests.Response()
            response.status_code = code
            response._content = b"SECRET_RESPONSE_TOKEN"
            with patch.object(client, "_get", return_value=response), self.assertLogs("etl.auth", level="WARNING") as captured:
                self.assertIsNone(client.get_guest_token("uuid", force_refresh=True))
            self.assertIn(str(code), "\n".join(captured.output))
            self.assertNotIn("SECRET_RESPONSE_TOKEN", "\n".join(captured.output))

    def test_invalid_source_config_and_unwritable_output(self):
        self.source.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigurationError, "Source config"):
            config.startup_check(self.source, self.root / "out", emit=lambda _: None)
        self.source.write_text("{}", encoding="utf-8")
        block = self.root / "blocked"
        block.write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigurationError, "not writable"):
            config.startup_check(self.source, block, emit=lambda _: None)

    def test_smoke_is_offline(self):
        from tests import smoke_config
        with patch("sys.argv", ["smoke_config", "--config", str(self.source), "--output-dir", str(self.root / "out")]), \
             patch("requests.sessions.Session.request", side_effect=AssertionError("must stay offline")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(smoke_config.main(), 0)
        self.assertIn("Standalone imports: OK", output.getvalue())

    def test_progress_shows_source_success_skip_and_failure(self):
        from extractor import extract_all
        from tests.helpers import FakeAdapter, record, http_error, FIXED_TIME
        adapter = FakeAdapter([record("1"), record("2"), record("3")], {"2": http_error(404)})
        targets = adapter.discover()
        targets[2].options["skip_reason"] = "known-bad fixture"
        with patch.object(adapter, "discover", return_value=targets), self.assertLogs("etl.extractor", level="INFO") as captured:
            extract_all(adapters=[adapter], now=FIXED_TIME)
        output = "\n".join(captured.output)
        self.assertIn("[1/1]", output)
        self.assertIn("OK (1 rows", output)
        self.assertIn("SKIPPED", output)
        self.assertIn("http_404", output)
