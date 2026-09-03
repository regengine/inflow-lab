import httpx
import pytest

from app.build_info import APP_VERSION
from scripts.browser_smoke import _browser_context_options, _check_healthz_build, _load_config


def test_browser_smoke_config_uses_browser_env(monkeypatch):
    monkeypatch.setenv("REGENGINE_BROWSER_BASE_URL", "https://demo.example.test/")
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BROWSER_PASSWORD", "demo-pass")
    monkeypatch.setenv("REGENGINE_BROWSER_TENANT", "browser-smoke")
    monkeypatch.setenv("REGENGINE_BROWSER_EXPECTED_BUILD_SHA", "abcdef1234567890")

    config = _load_config()

    assert config.base_url == "https://demo.example.test/"
    assert config.username == "demo-user"
    assert config.password == "demo-pass"
    assert config.tenant == "browser-smoke"
    assert config.expected_build_sha == "abcdef1234567890"
    assert _browser_context_options(config) == {
        "http_credentials": {
            "username": "demo-user",
            "password": "demo-pass",
        },
        "extra_http_headers": {"X-RegEngine-Tenant": "browser-smoke"},
    }


def test_browser_smoke_config_falls_back_to_remote_env(monkeypatch):
    monkeypatch.setenv("REGENGINE_REMOTE_BASE_URL", "https://railway.example.test")
    monkeypatch.setenv("REGENGINE_REMOTE_USERNAME", "remote-user")
    monkeypatch.setenv("REGENGINE_REMOTE_PASSWORD", "remote-pass")
    monkeypatch.setenv("REGENGINE_REMOTE_TENANT", "remote-browser-smoke")
    monkeypatch.setenv("REGENGINE_EXPECTED_BUILD_SHA", "123456abcdef")

    config = _load_config()

    assert config.base_url == "https://railway.example.test"
    assert config.username == "remote-user"
    assert config.password == "remote-pass"
    assert config.tenant == "remote-browser-smoke"
    assert config.expected_build_sha == "123456abcdef"


def test_browser_smoke_config_requires_username_password_pair(monkeypatch):
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "demo-user")
    monkeypatch.delenv("REGENGINE_BROWSER_PASSWORD", raising=False)
    monkeypatch.delenv("REGENGINE_REMOTE_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="must be provided together"):
        _load_config()


def test_browser_smoke_checks_expected_healthz_build(monkeypatch):
    def fake_get(url, timeout):
        assert url == "https://demo.example.test/api/healthz"
        assert timeout == 5.0
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "ok": True,
                "build": {
                    "version": APP_VERSION,
                    "commit_sha": "abcdef1234567890",
                },
            },
        )

    monkeypatch.setattr("scripts.browser_smoke.httpx.get", fake_get)

    _check_healthz_build("https://demo.example.test", "abcdef1")


def test_browser_smoke_rejects_stale_healthz_build(monkeypatch):
    monkeypatch.setattr(
        "scripts.browser_smoke.httpx.get",
        lambda url, timeout: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "ok": True,
                "build": {
                    "version": APP_VERSION,
                    "commit_sha": "abcdef1234567890",
                },
            },
        ),
    )

    with pytest.raises(RuntimeError, match="build commit mismatch"):
        _check_healthz_build("https://demo.example.test", "fedcba9876543210")


def test_browser_smoke_config_rejects_username_and_password_from_different_sources(monkeypatch):
    """A BROWSER username must never be paired with a REMOTE password.

    The half-updated environment this guards against — a fresh
    REGENGINE_BROWSER_USERNAME next to a stale REGENGINE_REMOTE_PASSWORD left
    over from an earlier remote_smoke/live_trial session — used to build a
    credential pair that existed in no source, and failed later as a 401.
    """
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "browser-user")
    monkeypatch.delenv("REGENGINE_BROWSER_PASSWORD", raising=False)
    monkeypatch.delenv("REGENGINE_REMOTE_USERNAME", raising=False)
    monkeypatch.setenv("REGENGINE_REMOTE_PASSWORD", "stale-remote-password")

    with pytest.raises(RuntimeError, match="must be provided together"):
        _load_config()


def test_browser_smoke_config_rejects_remote_password_without_remote_username(monkeypatch):
    monkeypatch.delenv("REGENGINE_BROWSER_USERNAME", raising=False)
    monkeypatch.delenv("REGENGINE_BROWSER_PASSWORD", raising=False)
    monkeypatch.delenv("REGENGINE_REMOTE_USERNAME", raising=False)
    monkeypatch.setenv("REGENGINE_REMOTE_PASSWORD", "remote-pass")

    with pytest.raises(RuntimeError, match="REGENGINE_REMOTE_USERNAME"):
        _load_config()


def test_browser_smoke_config_allows_no_credentials_at_all(monkeypatch):
    """Local mode runs against a no-auth server and must stay credential-free."""
    for name in (
        "REGENGINE_BROWSER_USERNAME",
        "REGENGINE_BROWSER_PASSWORD",
        "REGENGINE_REMOTE_USERNAME",
        "REGENGINE_REMOTE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    config = _load_config()

    assert config.username is None
    assert config.password is None
    assert "http_credentials" not in _browser_context_options(config)


def test_browser_smoke_shares_one_sha_prefix_match_implementation():
    from scripts import _smoke_common, browser_smoke, remote_smoke

    assert browser_smoke._sha_prefix_match is _smoke_common.sha_prefix_match
    assert remote_smoke._sha_prefix_match is _smoke_common.sha_prefix_match
