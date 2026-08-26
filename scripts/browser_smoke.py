from __future__ import annotations

import os
import socket
import subprocess  # nosec B404
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imported after the sys.path bootstrap above so
# `python scripts/browser_smoke.py` works from a clean checkout; hence the E402
# waivers.
from app.build_info import APP_VERSION  # noqa: E402
from scripts import _smoke_common as smoke  # noqa: E402


# Basic Auth credential sources, most specific first. Each entry is a *pair*:
# a username and a password are only ever used together with the partner they
# were configured alongside. Mixing a fresh REGENGINE_BROWSER_USERNAME with a
# stale REGENGINE_REMOTE_PASSWORD left over from an earlier remote_smoke or
# live_trial session produced a credential pair that never existed in any
# source, and the run then failed with a 401 that read as "the console is
# broken" rather than "your environment does not match".
CREDENTIAL_SOURCES = (
    ("REGENGINE_BROWSER_USERNAME", "REGENGINE_BROWSER_PASSWORD"),
    ("REGENGINE_REMOTE_USERNAME", "REGENGINE_REMOTE_PASSWORD"),
)

# The imported row must land inside RegEngine's replay window
# (WEBHOOK_MAX_EVENT_AGE_DAYS=90), otherwise the mock rejects it outright and
# the smoke never reaches the KDE-warning assertion it exists to make. Built
# relative to "now" rather than pinned to a calendar date so it cannot go
# stale again; a day back is recent enough to always be in-window and old
# enough to never trip the 24h future ceiling.
IMPORT_CSV_AGE = timedelta(days=1)


def import_csv_with_kde_warnings(now: datetime | None = None) -> str:
    """A one-row scheduled-events CSV that is valid except for missing KDEs."""
    moment = (now or datetime.now(UTC)) - IMPORT_CSV_AGE
    timestamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    harvest_date = moment.date().isoformat()
    return (
        "cte_type,traceability_lot_code,product_description,quantity,"
        "unit_of_measure,location_name,timestamp,kdes\n"
        f"harvesting,TLC-BROWSER-WARN,Romaine Lettuce,10,cases,Valley Fresh Farms,"
        f'{timestamp},"{{""harvest_date"":""{harvest_date}""}}"\n'
    )


@dataclass(frozen=True, slots=True)
class BrowserSmokeConfig:
    base_url: str | None
    headless: bool
    username: str | None
    password: str | None
    tenant: str | None
    expected_build_sha: str | None
    executable_path: str | None = None


def main() -> int:
    config = _load_config()
    with _base_url(config.base_url) as base_url:
        _run_dashboard_smoke(base_url=base_url, config=config)
    print("Browser smoke passed.")
    return 0


def _load_config() -> BrowserSmokeConfig:
    username, password = _load_credentials()

    return BrowserSmokeConfig(
        base_url=_env_text("REGENGINE_BROWSER_BASE_URL") or _env_text("REGENGINE_REMOTE_BASE_URL"),
        headless=os.getenv("REGENGINE_BROWSER_HEADLESS", "1").lower() not in {"0", "false", "no"},
        username=username,
        password=password,
        tenant=_env_text("REGENGINE_BROWSER_TENANT") or _env_text("REGENGINE_REMOTE_TENANT"),
        expected_build_sha=_env_text("REGENGINE_BROWSER_EXPECTED_BUILD_SHA")
        or _env_text("REGENGINE_EXPECTED_BUILD_SHA"),
        executable_path=_env_text("REGENGINE_BROWSER_EXECUTABLE"),
    )


def _load_credentials() -> tuple[str | None, str | None]:
    """Resolve Basic Auth as a pair from a single source, never field by field.

    The first source with *either* half set wins, and that source must supply
    both halves. See CREDENTIAL_SOURCES.
    """
    for username_var, password_var in CREDENTIAL_SOURCES:
        username = _env_text(username_var)
        password = _env_text(password_var)
        if username is None and password is None:
            continue
        if not username or not password:
            raise RuntimeError(
                f"{username_var} and {password_var} must be provided together"
            )
        return username, password
    return None, None


@contextmanager
def _base_url(configured_base_url: str | None) -> Iterator[str]:
    if configured_base_url:
        yield configured_base_url.rstrip("/")
        return

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="regengine-browser-smoke-") as temp_dir:
        env = os.environ.copy()
        env["REGENGINE_DATA_DIR"] = str(Path(temp_dir) / "data")
        env["REGENGINE_CORS_ORIGINS"] = base_url
        env.pop("REGENGINE_BASIC_AUTH_USERNAME", None)
        env.pop("REGENGINE_BASIC_AUTH_PASSWORD", None)
        # Local smoke harness starts a fixed uvicorn argv with no user input.
        process = subprocess.Popen(  # nosec B603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_healthz(base_url, process)
            yield base_url
        finally:
            _terminate(process)


def _run_dashboard_smoke(base_url: str, config: BrowserSmokeConfig) -> None:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: uv sync --group browser "
            "&& uv run playwright install chromium"
        ) from exc

    _check_healthz_build(base_url, config.expected_build_sha)

    output_dir = Path("output/playwright")
    console_errors: list[str] = []
    page = None
    failed = False
    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, object] = {"headless": config.headless}
            if config.executable_path:
                launch_options["executable_path"] = config.executable_path
            browser = playwright.chromium.launch(**launch_options)
            context_options = _browser_context_options(config)
            context = browser.new_context(**context_options)
            page = context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: console_errors.append(str(error)))

            page.goto(base_url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="Plant Operations Console", exact=True)).to_be_visible()
            expect(page.locator("#guideRail")).to_contain_text("How to use this console")
            expect(page.locator('#guideRail [data-guide-step="setup"]')).to_be_visible()

            # First visit shows the onboarding welcome; walk into the guided
            # tour, navigate forward and back, then skip out of it.
            expect(page.locator("#welcomeOverlay")).to_be_visible()
            page.locator("#welcomeTourBtn").click()
            expect(page.locator("#welcomeOverlay")).to_be_hidden()
            expect(page.locator("#tourPopover")).to_be_visible()
            expect(page.locator("#tourProgress")).to_have_text("Step 1 of 5")
            page.locator("#tourNextBtn").click()
            expect(page.locator("#tourProgress")).to_have_text("Step 2 of 5")
            page.locator("#tourBackBtn").click()
            expect(page.locator("#tourProgress")).to_have_text("Step 1 of 5")
            page.locator("#tourSkipBtn").click()
            expect(page.locator("#tourPopover")).to_be_hidden()

            # Onboarding is persisted, so a reload must not re-interrupt.
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="Plant Operations Console", exact=True)).to_be_visible()
            expect(page.locator("#welcomeOverlay")).to_be_hidden()

            page.locator("#advancedConfig").evaluate("element => { element.open = true; }")
            page.locator("#batchSize").fill("1")
            page.locator("#interval").fill("0.1")
            page.locator("#deliveryMode").select_option("mock")
            page.locator("#endpoint").fill("")
            page.locator("#apiKey").fill("")
            page.locator("#tenantId").fill("")

            # Pressing Enter inside the setup form must not trigger the
            # implicit form submission (which used to reload the page).
            page.evaluate("window.__smokeMarker = true")
            page.locator("#source").press("Enter")
            page.wait_for_timeout(300)
            if page.evaluate("window.__smokeMarker") is not True:
                raise RuntimeError("Enter inside the setup form reloaded the page")

            # Start/Pause mirror the loop state: pausing is only possible
            # while running, starting only while idle.
            expect(page.locator("#stopBtn")).to_be_disabled()
            page.locator("#startBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Started production line")
            expect(page.locator("#startBtn")).to_be_disabled()
            page.locator("#stopBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Paused production line")

            # Clear shift is two-step: first click arms, second click wipes.
            page.locator("#resetBtn").click()
            expect(page.locator("#resetBtn")).to_contain_text("Confirm clear shift")
            page.locator("#resetBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Cleared line state")

            page.locator("#stepBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Recorded and posted")
            expect(page.locator("#eventsBody tr")).to_have_count(1)

            page.locator("#testConnectionBtn").click()
            expect(page.locator("#connectionResult")).to_contain_text("mock")

            page.locator('#demoFixture option[value="fresh_cut_transformation"]').wait_for(state="attached")
            page.locator("#demoFixture").select_option("fresh_cut_transformation")
            page.locator("#loadFixtureBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Loaded line data and posted")
            expect(page.locator("#eventsBody")).to_contain_text("TLC-DEMO-FC-OUT-001")

            page.locator("#lotLookup").fill("TLC-DEMO-FC-OUT-001")
            page.locator("#lineageBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Loaded lineage for TLC-DEMO-FC-OUT-001")
            expect(page.locator("#lineageResults")).to_contain_text("TLC-DEMO-FC-OUT-001")
            expect(page.locator("#lineageResults")).to_contain_text("Transformation")

            csv_path = output_dir / "browser_smoke_import.csv"
            output_dir.mkdir(parents=True, exist_ok=True)
            csv_path.write_text(import_csv_with_kde_warnings(), encoding="utf-8")
            page.locator("#csvImportType").select_option("scheduled_events")
            page.locator("#csvFile").set_input_files(str(csv_path))
            page.locator("#importCsvBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("warning")
            expect(page.locator("#importResults")).to_contain_text("Missing expected harvesting KDE: reference_document")

            # Enter in the quick-trace box traces the lot without the button.
            page.locator("#lotLookup").press("Enter")
            expect(page.locator("#statusMessage")).to_contain_text("Loaded lineage for TLC-DEMO-FC-OUT-001")

            browser.close()
    except Exception:
        failed = True
        if page is not None:
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output_dir / "browser_smoke_failure.png"), full_page=True)
            except Exception:
                console_errors.append("Could not capture failure screenshot")
        raise
    finally:
        if console_errors and not failed:
            raise RuntimeError(f"Browser console errors: {console_errors}")


def _browser_context_options(config: BrowserSmokeConfig) -> dict[str, object]:
    options: dict[str, object] = {}
    if config.username and config.password:
        options["http_credentials"] = {
            "username": config.username,
            "password": config.password,
        }
    if config.tenant:
        options["extra_http_headers"] = {"X-RegEngine-Tenant": config.tenant}
    return options


def _check_healthz_build(base_url: str, expected_build_sha: str | None) -> None:
    response = httpx.get(f"{base_url}/api/healthz", timeout=5.0)
    response.raise_for_status()
    payload = response.json()
    build = payload.get("build")
    if not isinstance(build, dict):
        raise RuntimeError("/api/healthz did not include build metadata")
    if build.get("version") != APP_VERSION:
        raise RuntimeError(
            f"/api/healthz build version mismatch: expected {APP_VERSION}, got {build.get('version')!r}"
        )
    if expected_build_sha:
        actual = build.get("commit_sha")
        if not isinstance(actual, str) or not actual:
            raise RuntimeError(
                f"/api/healthz build commit mismatch: expected {expected_build_sha[:12]}, got none"
            )
        if not _sha_prefix_match(actual, expected_build_sha):
            raise RuntimeError(
                f"/api/healthz build commit mismatch: expected {expected_build_sha[:12]}, got {actual[:12]}"
            )


# Single definition lives in scripts/_smoke_common.py.
_sha_prefix_match = smoke.sha_prefix_match


def _wait_for_healthz(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise RuntimeError(
                f"Local server exited before browser smoke. stdout={stdout.strip()} stderr={stderr.strip()}"
            )
        try:
            response = httpx.get(f"{base_url}/api/healthz", timeout=1.0)
            if response.status_code == 200 and response.json().get("ok") is True:
                return
            last_error = f"status={response.status_code}"
        except Exception as exc:  # pragma: no cover - only for process startup timing
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {base_url}/api/healthz: {last_error}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _env_text(name: str) -> str | None:
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    return None


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Browser smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
