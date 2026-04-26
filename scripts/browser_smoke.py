from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx


CSV_WITH_KDE_WARNINGS = """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,kdes
harvesting,TLC-BROWSER-WARN,Romaine Lettuce,10,cases,Valley Fresh Farms,2026-02-10T08:00:00Z,"{""harvest_date"":""2026-02-10""}"
"""


@dataclass(frozen=True, slots=True)
class BrowserSmokeConfig:
    base_url: str | None
    headless: bool
    username: str | None
    password: str | None
    tenant: str | None


def main() -> int:
    config = _load_config()
    with _base_url(config.base_url) as base_url:
        _run_dashboard_smoke(base_url=base_url, config=config)
    print("Browser smoke passed.")
    return 0


def _load_config() -> BrowserSmokeConfig:
    username = _env_text("REGENGINE_BROWSER_USERNAME") or _env_text("REGENGINE_REMOTE_USERNAME")
    password = _env_text("REGENGINE_BROWSER_PASSWORD") or _env_text("REGENGINE_REMOTE_PASSWORD")
    if bool(username) != bool(password):
        raise RuntimeError(
            "REGENGINE_BROWSER_USERNAME and REGENGINE_BROWSER_PASSWORD must be provided together"
        )

    return BrowserSmokeConfig(
        base_url=_env_text("REGENGINE_BROWSER_BASE_URL") or _env_text("REGENGINE_REMOTE_BASE_URL"),
        headless=os.getenv("REGENGINE_BROWSER_HEADLESS", "1").lower() not in {"0", "false", "no"},
        username=username,
        password=password,
        tenant=_env_text("REGENGINE_BROWSER_TENANT") or _env_text("REGENGINE_REMOTE_TENANT"),
    )


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
        process = subprocess.Popen(
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
            "Playwright is not installed. Run: python3 -m pip install -r requirements-browser.txt "
            "&& python3 -m playwright install chromium"
        ) from exc

    output_dir = Path("output/playwright")
    console_errors: list[str] = []
    page = None
    failed = False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=config.headless)
            context_options = _browser_context_options(config)
            context = browser.new_context(**context_options)
            page = context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: console_errors.append(str(error)))

            page.goto(base_url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="RegEngine Inflow Lab")).to_be_visible()

            page.locator("#batchSize").fill("1")
            page.locator("#interval").fill("0.1")
            page.locator("#deliveryMode").select_option("mock")
            page.locator("#endpoint").fill("")
            page.locator("#apiKey").fill("")
            page.locator("#tenantId").fill("")
            page.locator("#stopBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Stopped simulator loop")

            page.locator("#startBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Started simulator loop")
            page.locator("#stopBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Stopped simulator loop")

            page.locator("#resetBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Reset simulator state")

            page.locator("#stepBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Generated and posted")
            expect(page.locator("#eventsBody tr")).to_have_count(1)

            page.locator('#demoFixture option[value="fresh_cut_transformation"]').wait_for(state="attached")
            page.locator("#demoFixture").select_option("fresh_cut_transformation")
            page.locator("#loadFixtureBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Loaded fixture and posted")
            expect(page.locator("#eventsBody")).to_contain_text("TLC-DEMO-FC-OUT-001")

            page.locator("#lotLookup").fill("TLC-DEMO-FC-OUT-001")
            page.locator("#lineageBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("Loaded lineage for TLC-DEMO-FC-OUT-001")
            expect(page.locator("#lineageResults")).to_contain_text("TLC-DEMO-FC-OUT-001")
            expect(page.locator("#lineageResults")).to_contain_text("Transformation")

            csv_path = output_dir / "browser_smoke_import.csv"
            output_dir.mkdir(parents=True, exist_ok=True)
            csv_path.write_text(CSV_WITH_KDE_WARNINGS, encoding="utf-8")
            page.locator("#csvImportType").select_option("scheduled_events")
            page.locator("#csvFile").set_input_files(str(csv_path))
            page.locator("#importCsvBtn").click()
            expect(page.locator("#statusMessage")).to_contain_text("warning")
            expect(page.locator("#importResults")).to_contain_text("Missing expected harvesting KDE: farm_location")

            browser.close()
    except Exception:
        failed = True
        if page is not None:
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output_dir / "browser_smoke_failure.png"), full_page=True)
            except Exception:
                pass
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
