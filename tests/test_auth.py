import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import requests
from dwh_client import DWHClient
from extractors.base import tls_verify
from extractor import classify_error
from tests.test_adapters import response

class AuthTests(unittest.TestCase):
    def test_cookie_domains_do_not_raise_conflict(self):
        client = DWHClient()
        client._get = Mock(return_value=response())
        client.session.cookies.set("XSRF-TOKEN", "portal%20token", domain="idmc.jogjaprov.go.id")
        client.session.cookies.set("XSRF-TOKEN", "wrong", domain="dwh.jogjaprov.go.id")
        client.load_base_cookies()
        self.assertEqual(client.session.headers["X-XSRF-TOKEN"], "portal token")

    def test_guest_token_refresh_after_401(self):
        client = DWHClient()
        client.get_guest_token = Mock(side_effect=["old", "new"])
        client._get = Mock(side_effect=[response(status=401), response({"ok": True})])
        client._authed_get("https://dwh.jogjaprov.go.id/api/v1/chart/1", "uuid", context="test")
        client.get_guest_token.assert_any_call("uuid", force_refresh=True)
        self.assertEqual(client._get.call_args.kwargs["headers"]["X-GuestToken"], "new")

    def test_dynamic_token_shape_and_transient_error_propagation(self):
        client = DWHClient()
        client._get = Mock(return_value=response({"data": {"guestToken": "fake-token"}}))
        with patch("dwh_client._save_cached_token") as save:
            self.assertEqual(client.get_guest_token("uuid", force_refresh=True), "fake-token")
            save.assert_called_once_with("uuid", "fake-token")
        client._get = Mock(side_effect=requests.Timeout())
        with self.assertRaises(requests.Timeout):
            client.get_guest_token("uuid", force_refresh=True)

    def test_tls_explicit_override_and_error_category(self):
        with patch("ingestion_config.get_config", return_value=SimpleNamespace(verify=True, app_env="development")):
            self.assertTrue(tls_verify())
        with patch("ingestion_config.get_config", return_value=SimpleNamespace(verify=False, app_env="development")), patch("ingestion_config._tls_warning_emitted", False):
            with self.assertLogs("etl.config", level="WARNING"):
                self.assertFalse(tls_verify())
        self.assertEqual(classify_error(requests.exceptions.SSLError()), "tls")
