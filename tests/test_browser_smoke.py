import sys
import types
from pathlib import Path

import httpx
import pytest

from app.build_info import APP_VERSION
from scripts.browser_smoke import (
    BrowserSmokeConfig,
    _browser_context_options,
    _check_healthz_build,
    _load_config,
    _run_dashboard_smoke,
)
from scripts.remote_smoke import RemoteSmokeFailure


def test_browser_smoke_config_uses_browser_env(monkeypatch):
    # #124: _load_config refuses an off-allowlist host once credentials are
    # present. These tests cover env precedence, not host policy.
    monkeypatch.setenv("REGENGINE_REMOTE_ALLOWED_HOSTS", "demo.example.test")
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
    monkeypatch.setenv("REGENGINE_REMOTE_ALLOWED_HOSTS", "railway.example.test")
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


def test_browser_smoke_config_rejects_a_cross_source_credential_pair(monkeypatch):
    # A freshly set browser username plus a REMOTE password left exported by
    # an earlier remote_smoke or live_trial session (#191). Each field is
    # present, but they never appeared together in one source.
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "local-test-user")
    monkeypatch.delenv("REGENGINE_BROWSER_PASSWORD", raising=False)
    monkeypatch.delenv("REGENGINE_REMOTE_USERNAME", raising=False)
    monkeypatch.setenv("REGENGINE_REMOTE_PASSWORD", "stale-demo-password-from-a-different-job")

    with pytest.raises(
        RuntimeError,
        match="REGENGINE_BROWSER_USERNAME and REGENGINE_BROWSER_PASSWORD must be provided together",
    ):
        _load_config()


def test_browser_smoke_config_rejects_the_reverse_cross_source_pair(monkeypatch):
    monkeypatch.delenv("REGENGINE_BROWSER_USERNAME", raising=False)
    monkeypatch.setenv("REGENGINE_BROWSER_PASSWORD", "browser-pass")
    monkeypatch.setenv("REGENGINE_REMOTE_USERNAME", "remote-user")
    monkeypatch.delenv("REGENGINE_REMOTE_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="must be provided together"):
        _load_config()


def test_browser_smoke_config_takes_the_browser_pair_whole(monkeypatch):
    # Both pairs complete: the browser pair wins, and no field is sourced
    # from the remote one.
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "browser-user")
    monkeypatch.setenv("REGENGINE_BROWSER_PASSWORD", "browser-pass")
    monkeypatch.setenv("REGENGINE_REMOTE_USERNAME", "remote-user")
    monkeypatch.setenv("REGENGINE_REMOTE_PASSWORD", "remote-pass")

    config = _load_config()

    assert (config.username, config.password) == ("browser-user", "browser-pass")


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


def test_browser_smoke_refuses_an_off_allowlist_base_url(monkeypatch):
    """#124: remote-browser-smoke.yml takes the same unconstrained base_url
    input, and _browser_context_options attaches the shared-demo credentials
    to every request via Playwright's http_credentials. The guard has to run
    at config load, before that context is ever built.
    """
    monkeypatch.delenv("REGENGINE_REMOTE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("REGENGINE_BROWSER_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BROWSER_PASSWORD", "demo-pass")

    with pytest.raises(RemoteSmokeFailure, match="Refusing to send credentials"):
        _load_config()


def test_browser_smoke_still_allows_the_local_spawn_path(monkeypatch):
    """base_url unset is the local-spawn path in _base_url(), which starts
    its own uvicorn on loopback -- it must not be caught by the allowlist.
    """
    monkeypatch.delenv("REGENGINE_REMOTE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("REGENGINE_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("REGENGINE_REMOTE_BASE_URL", raising=False)
    monkeypatch.setenv("REGENGINE_BROWSER_USERNAME", "demo-user")
    monkeypatch.setenv("REGENGINE_BROWSER_PASSWORD", "demo-pass")

    config = _load_config()

    assert config.base_url is None


# --- #133: the failure screenshot has to exist before CI can upload it -------
#
# `browser-smoke` in ci.yml and remote-browser-smoke.yml now upload
# output/playwright/ on failure. That artifact is only worth anything if the
# smoke actually writes the screenshot, and for the whole life of the capture
# it did not: the `except` that called page.screenshot() sat *outside*
# `with sync_playwright()`, so it ran only after that __exit__ had already
# stopped the driver. Every call raised "Event loop is closed! Is Playwright
# already stopped?", the bare `except Exception` swallowed it, and the run died
# with no file. Nothing failed loudly, which is why it survived.
#
# These two tests fake the driver rather than launching a browser: playwright
# is in the `browser` dependency group, and the `pytest` job installs only
# `dev`. The fake stops on __exit__ exactly like the real one, so a capture
# moved back outside the context manager fails here again.


class _PlaywrightStopped(RuntimeError):
    """What the real driver raises once sync_playwright() has exited."""


class _FakeDriver:
    def __init__(self) -> None:
        self.stopped = False

    def check_alive(self) -> None:
        if self.stopped:
            raise _PlaywrightStopped("Event loop is closed! Is Playwright already stopped?")


class _FakeLocator:
    """Accepts any locator call and stays chainable, so the smoke's ~90 lines
    of page interaction run untouched."""

    def __init__(self, driver: _FakeDriver) -> None:
        self._driver = driver

    def __getattr__(self, _name: str):
        def _call(*_args, **_kwargs):
            self._driver.check_alive()
            return self

        return _call


class _FakePage:
    def __init__(self, driver: _FakeDriver, fail_with: Exception | None = None) -> None:
        self._driver = driver
        self._fail_with = fail_with
        self.screenshot_paths: list[Path] = []

    def goto(self, *_args, **_kwargs) -> None:
        self._driver.check_alive()
        if self._fail_with is not None:
            raise self._fail_with

    def evaluate(self, script: str, *_args, **_kwargs):
        self._driver.check_alive()
        # The smoke reads this one back to prove Enter did not reload the page.
        return True if script == "window.__smokeMarker" else None

    def screenshot(self, path: str, full_page: bool = False) -> None:
        self._driver.check_alive()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.screenshot_paths.append(destination)

    def __getattr__(self, _name: str):
        def _call(*_args, **_kwargs):
            self._driver.check_alive()
            return _FakeLocator(self._driver)

        return _call


class _FakeBrowser:
    def __init__(self, driver: _FakeDriver, page: _FakePage) -> None:
        self._driver = driver
        self._page = page

    def new_context(self, **_kwargs) -> "_FakeBrowser":
        return self

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        self._driver.check_alive()


class _FakeSyncPlaywright:
    def __init__(self, driver: _FakeDriver, page: _FakePage) -> None:
        self._driver = driver
        self._page = page

    def __enter__(self) -> "_FakeSyncPlaywright":
        return self

    def __exit__(self, *_exc_info) -> bool:
        # The real context manager stops the driver here. This single line is
        # the whole reason the screenshot has to be taken further in.
        self._driver.stopped = True
        return False

    @property
    def chromium(self) -> "_FakeSyncPlaywright":
        return self

    def launch(self, **_kwargs) -> _FakeBrowser:
        return _FakeBrowser(self._driver, self._page)


def _install_fake_playwright(monkeypatch, page: _FakePage, driver: _FakeDriver) -> None:
    module = types.ModuleType("playwright.sync_api")
    # expect(locator).to_*() is chainable on the fake locator itself.
    module.expect = lambda locator: locator
    module.sync_playwright = lambda: _FakeSyncPlaywright(driver, page)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(
        "scripts.browser_smoke._check_healthz_build", lambda *_args, **_kwargs: None
    )


def _smoke_config() -> BrowserSmokeConfig:
    return BrowserSmokeConfig(
        base_url="http://127.0.0.1:9999",
        headless=True,
        username=None,
        password=None,
        tenant=None,
        expected_build_sha=None,
    )


def test_browser_smoke_captures_the_failure_screenshot_before_the_driver_stops(
    tmp_path, monkeypatch
):
    # output_dir is relative to the working directory, which on a runner is the
    # workspace root the upload step's `path: output/playwright/` resolves from.
    monkeypatch.chdir(tmp_path)
    driver = _FakeDriver()
    page = _FakePage(driver, fail_with=RuntimeError("simulated smoke assertion failure"))
    _install_fake_playwright(monkeypatch, page, driver)

    with pytest.raises(RuntimeError, match="simulated smoke assertion failure"):
        _run_dashboard_smoke(base_url="http://127.0.0.1:9999", config=_smoke_config())

    assert driver.stopped, "the fake driver must stop, or this proves nothing"
    screenshot = tmp_path / "output" / "playwright" / "browser_smoke_failure.png"
    assert screenshot.is_file(), (
        "A failing browser smoke must leave output/playwright/browser_smoke_failure.png "
        "for CI to upload (#133). An empty artifact means the capture ran after "
        "`with sync_playwright()` exited, where the driver is already stopped and "
        "page.screenshot() can only raise."
    )


def test_browser_smoke_writes_no_failure_screenshot_when_it_passes(tmp_path, monkeypatch):
    # The other half of #133: the upload has to be a no-op on a green run. The
    # workflow guards it with `if: failure()`; this guards the file itself, so
    # a stray screenshot cannot start showing up on passing runs.
    monkeypatch.chdir(tmp_path)
    driver = _FakeDriver()
    page = _FakePage(driver)
    _install_fake_playwright(monkeypatch, page, driver)

    _run_dashboard_smoke(base_url="http://127.0.0.1:9999", config=_smoke_config())

    assert page.screenshot_paths == []
    assert not (tmp_path / "output" / "playwright" / "browser_smoke_failure.png").exists()
