import pytest

from scripts.browser_smoke import _browser_context_options, _load_config


def test_browser_smoke_config_uses_browser_env(monkeypatch):
    monkeypatch.setenv("REGENGINE_BROWSER_BASE_URL", "https://demo.example.test/")
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BROWSER_PASSWORD", "demo-pass")
    monkeypatch.setenv("REGENGINE_BROWSER_TENANT", "browser-smoke")

    config = _load_config()

    assert config.base_url == "https://demo.example.test/"
    assert config.username == "demo-user"
    assert config.password == "demo-pass"
    assert config.tenant == "browser-smoke"
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

    config = _load_config()

    assert config.base_url == "https://railway.example.test"
    assert config.username == "remote-user"
    assert config.password == "remote-pass"
    assert config.tenant == "remote-browser-smoke"


def test_browser_smoke_config_requires_username_password_pair(monkeypatch):
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "demo-user")
    monkeypatch.delenv("REGENGINE_BROWSER_PASSWORD", raising=False)
    monkeypatch.delenv("REGENGINE_REMOTE_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="must be provided together"):
        _load_config()
