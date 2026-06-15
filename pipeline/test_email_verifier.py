"""
Unit tests for email_verifier.py (RMP #68)

Isolates from real pipeline.db by patching DB_PATH to a temp file.
All requests.post calls are mocked — no network, no quota burn.
Mock payloads use the real MailValid nested shape:
  {"success": true, "credits_used": 1, "result": {"status": ..., "is_disposable": ...}}
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Create temp DB BEFORE importing email_verifier
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db_path = _tmp_db.name
_tmp_db.close()


def _make_response(status_code=200, json_data=None):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


def _mailvalid_body(status, is_disposable=False, status_reason=None):
    """Build a MailValid-shaped response body with nested 'result'."""
    body = {
        "success": True,
        "credits_used": 1,
        "result": {
            "status": status,
            "is_disposable": is_disposable,
        },
    }
    if status_reason is not None:
        body["result"]["status_reason"] = status_reason
    return body


def _ensure_table():
    """Create the cache table in the temp DB (mirrors _get_conn)."""
    conn = sqlite3.connect(_tmp_db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_cache (
            email       TEXT PRIMARY KEY,
            verdict     TEXT NOT NULL,
            raw_status  TEXT,
            verified_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


class TestVerifyEmail(unittest.TestCase):
    """Test verify_email decision matrix, fail-open, and caching."""

    @classmethod
    def setUpClass(cls):
        # Ensure table exists before any test
        _ensure_table()

        # Evict cached modules so email_verifier re-imports config
        for mod in list(sys.modules):
            if mod in ("email_verifier", "config"):
                del sys.modules[mod]

        # Set env vars that config.py reads
        os.environ["EMAIL_VERIFY_API_KEY"] = "test_key_fake"
        os.environ["EMAIL_VERIFY_ENABLED"] = "true"

        # Import config first, override DB_PATH to temp
        import config
        config.DB_PATH = _tmp_db_path
        config.EMAIL_VERIFY_API_KEY = "test_key_fake"
        config.EMAIL_VERIFY_ENABLED = True

        # Now import email_verifier — override the already-bound names directly
        import email_verifier
        email_verifier.DB_PATH = _tmp_db_path
        email_verifier.EMAIL_VERIFY_API_KEY = "test_key_fake"
        email_verifier.EMAIL_VERIFY_ENABLED = True
        cls.mod = email_verifier

    @classmethod
    def tearDownClass(cls):
        os.unlink(_tmp_db_path)

    def setUp(self):
        """Clear the cache table and reset module state between tests."""
        conn = sqlite3.connect(_tmp_db_path)
        conn.execute("DELETE FROM email_verification_cache")
        conn.commit()
        conn.close()
        # Reset the no-key warning flag
        self.mod._no_key_warned = False

    # ------------------------------------------------------------------
    # 1. Status variant tests (nested response shape)
    # ------------------------------------------------------------------

    @patch("email_verifier.requests.post")
    def test_status_valid(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("valid"))
        should_send, verdict = self.mod.verify_email("good@example.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "valid")
        print("  PASS: status=valid → (True, 'valid')")

    @patch("email_verifier.requests.post")
    def test_status_invalid(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("invalid"))
        should_send, verdict = self.mod.verify_email("dead@example.com")
        self.assertFalse(should_send)
        self.assertEqual(verdict, "invalid")
        print("  PASS: status=invalid → (False, 'invalid')")

    @patch("email_verifier.requests.post")
    def test_status_do_not_mail(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("do_not_mail"))
        should_send, verdict = self.mod.verify_email("spam@example.com")
        self.assertFalse(should_send)
        self.assertEqual(verdict, "do_not_mail")
        print("  PASS: status=do_not_mail → (False, 'do_not_mail')")

    @patch("email_verifier.requests.post")
    def test_status_catch_all(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("catch_all"))
        should_send, verdict = self.mod.verify_email("any@catchall.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "catch_all")
        print("  PASS: status=catch_all → (True, 'catch_all')")

    @patch("email_verifier.requests.post")
    def test_status_unknown(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("unknown"))
        should_send, verdict = self.mod.verify_email("maybe@example.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "unknown")
        print("  PASS: status=unknown → (True, 'unknown')")

    @patch("email_verifier.requests.post")
    def test_disposable_overrides_valid(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("valid", is_disposable=True))
        should_send, verdict = self.mod.verify_email("temp@throwaway.io")
        self.assertFalse(should_send)
        self.assertEqual(verdict, "disposable")
        print("  PASS: is_disposable=true → (False, 'disposable') even if status=valid")

    @patch("email_verifier.requests.post")
    def test_do_not_mail_role_based_allowed(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("do_not_mail", status_reason="role_based"))
        should_send, verdict = self.mod.verify_email("info@company.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "role_based")
        print("  PASS: status=do_not_mail + status_reason=role_based → (True, 'role_based')")

    @patch("email_verifier.requests.post")
    def test_do_not_mail_complainer_blocked(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("do_not_mail", status_reason="complainer"))
        should_send, verdict = self.mod.verify_email("complainer@example.com")
        self.assertFalse(should_send)
        self.assertEqual(verdict, "do_not_mail")
        print("  PASS: status=do_not_mail + status_reason=complainer → (False, 'do_not_mail')")

    # ------------------------------------------------------------------
    # 2. Fail-open tests
    # ------------------------------------------------------------------

    @patch("email_verifier.requests.post")
    def test_timeout_fail_open(self, mock_post):
        import requests as req
        mock_post.side_effect = req.Timeout("timed out")
        should_send, verdict = self.mod.verify_email("timeout@example.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "api_error")
        print("  PASS: Timeout → (True, 'api_error')")

    @patch("email_verifier.requests.post")
    def test_connection_error_fail_open(self, mock_post):
        import requests as req
        mock_post.side_effect = req.ConnectionError("refused")
        should_send, verdict = self.mod.verify_email("connfail@example.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "api_error")
        print("  PASS: ConnectionError → (True, 'api_error')")

    @patch("email_verifier.requests.post")
    def test_http_500_fail_open(self, mock_post):
        mock_post.return_value = _make_response(500)
        should_send, verdict = self.mod.verify_email("servererr@example.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "api_error")
        print("  PASS: HTTP 500 → (True, 'api_error')")

    @patch("email_verifier.requests.post")
    def test_http_429_quota_fail_open(self, mock_post):
        mock_post.return_value = _make_response(429)
        should_send, verdict = self.mod.verify_email("quota@example.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "quota_exhausted")
        print("  PASS: HTTP 429 → (True, 'quota_exhausted')")

    @patch("email_verifier.requests.post")
    def test_missing_result_key_fail_open(self, mock_post):
        # 200 OK but no "result" key in body — must fail open, not crash
        mock_post.return_value = _make_response(200, {
            "success": True, "credits_used": 0,
        })
        should_send, verdict = self.mod.verify_email("noresult@example.com")
        self.assertTrue(should_send)
        self.assertEqual(verdict, "api_error")
        print("  PASS: 200 with no 'result' key → (True, 'api_error')")

    # ------------------------------------------------------------------
    # 3. Cache tests
    # ------------------------------------------------------------------

    @patch("email_verifier.requests.post")
    def test_cache_hit_skips_api(self, mock_post):
        mock_post.return_value = _make_response(200, _mailvalid_body("valid"))
        addr = "cached@example.com"

        # First call — hits API
        s1, v1 = self.mod.verify_email(addr)
        self.assertTrue(s1)
        self.assertEqual(v1, "valid")
        self.assertEqual(mock_post.call_count, 1)

        # Second call — should use cache, NOT call API again
        s2, v2 = self.mod.verify_email(addr)
        self.assertTrue(s2)
        self.assertEqual(v2, "valid")
        self.assertEqual(mock_post.call_count, 1)  # still 1
        print("  PASS: cache hit — requests.post called once for two verify_email calls")

    @patch("email_verifier.requests.post")
    def test_api_error_not_cached(self, mock_post):
        import requests as req
        # First call — timeout (api_error, should NOT be cached)
        mock_post.side_effect = req.Timeout("timed out")
        s1, v1 = self.mod.verify_email("retry@example.com")
        self.assertTrue(s1)
        self.assertEqual(v1, "api_error")
        self.assertEqual(mock_post.call_count, 1)

        # Second call — API recovers, should call again (not cached)
        mock_post.side_effect = None
        mock_post.return_value = _make_response(200, _mailvalid_body("valid"))
        s2, v2 = self.mod.verify_email("retry@example.com")
        self.assertTrue(s2)
        self.assertEqual(v2, "valid")
        self.assertEqual(mock_post.call_count, 2)  # called again
        print("  PASS: api_error not cached — retry succeeds on next call")


if __name__ == "__main__":
    unittest.main(verbosity=2)
