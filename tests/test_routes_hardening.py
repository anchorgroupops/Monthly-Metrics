"""Tests for the hardening helpers in src/webapp/routes.py."""

import json

import pytest

from src.webapp import routes


# ── _safe_script_json ────────────────────────────────────────────────────────

class TestSafeScriptJson:
    def test_escapes_script_close_tag(self):
        out = routes._safe_script_json({"x": "</script>"})
        # The literal "</script>" must not survive — otherwise it would close
        # the surrounding <script> element on the rendered page.
        assert "</script>" not in out
        # Round-trips back to the original through normal JSON parsing.
        assert json.loads(out) == {"x": "</script>"}

    def test_escapes_html_comment_tokens(self):
        out = routes._safe_script_json({"x": "<!-- y -->"})
        assert "<!--" not in out
        assert "-->" not in out
        assert json.loads(out) == {"x": "<!-- y -->"}

    def test_escapes_js_line_separators(self):
        s = "a b c"
        out = routes._safe_script_json({"x": s})
        # Raw separators would terminate JS string literals on some engines.
        assert " " not in out
        assert " " not in out
        assert json.loads(out)["x"] == s

    def test_round_trip_for_normal_payload(self):
        payload = {
            "labels": ["2026-01", "2026-02"],
            "metrics": {"pCVR": {"target": 0.035, "values": [0.03, 0.04]}},
        }
        assert json.loads(routes._safe_script_json(payload)) == payload


# ── _cookie_is_secure ────────────────────────────────────────────────────────

class TestCookieIsSecure:
    def test_https_url_yields_secure_cookie(self, mocker):
        mocker.patch("src.webapp.routes.WEB_BASE_URL", "https://metrics.example.com")
        assert routes._cookie_is_secure() is True

    def test_http_url_disables_secure_for_localhost_dev(self, mocker):
        mocker.patch("src.webapp.routes.WEB_BASE_URL", "http://localhost:8081")
        assert routes._cookie_is_secure() is False


# ── 500 handler ──────────────────────────────────────────────────────────────

class TestServerErrorHandler:
    def test_unhandled_exception_returns_clean_500(self, monkeypatch, tmp_db):
        monkeypatch.setattr("src.storage.DATABASE_PATH", tmp_db)
        monkeypatch.setattr("src.webapp.app.WEB_BASE_PATH", "")
        from fastapi.testclient import TestClient

        from src.webapp.app import create_app

        app = create_app()

        @app.get("/__boom__", include_in_schema=False)
        def _boom():  # pragma: no cover - intentionally raises
            raise RuntimeError("sentinel")

        # raise_server_exceptions=False asks the test transport to actually
        # invoke the registered exception handler instead of bubbling.
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/__boom__")
        assert r.status_code == 500
        assert "Something went wrong" in r.text
        # The clean page must NOT leak the exception type or message.
        assert "RuntimeError" not in r.text
        assert "sentinel" not in r.text
