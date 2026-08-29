"""Safety-net tests for the two operational scripts covered by #105 / #106.

Split from tests/test_live_trial.py (which we do not own and must not edit)
so these regressions have a home of their own:

  #105 -- scripts/live_trial.py --confirm-live must revert delivery to mock
  on every exit from the trial, not just the happy path, and a revert that
  itself fails must be reported loudly rather than swallowed.

  #106 -- scripts/cutover_preflight.sh must actually authenticate its proxy-
  contract probes, and must never report a path "ok" -- let alone the whole
  run PASSED -- when it could not tell a working contract from two services
  agreeing on the same failure.

Everything here runs against local fakes only: an in-process
httpx.MockTransport for live_trial.py (same approach test_live_trial.py
already uses), and a 127.0.0.1-bound stdlib HTTP server for
cutover_preflight.sh. Nothing here performs live delivery or contacts a real
external host.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from scripts.live_trial import main


REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "cutover_preflight.sh"

BASE_ENV = {
    "REGENGINE_REMOTE_BASE_URL": "https://demo.example.com",
    "REGENGINE_REMOTE_USERNAME": "demo",
    "REGENGINE_REMOTE_PASSWORD": "demo-password",
    "REGENGINE_REMOTE_TENANT": "live-trial-smoke",
}
LIVE_ENV = {
    **BASE_ENV,
    "REGENGINE_LIVE_ENDPOINT": "https://www.regengine.co/api/v1/webhooks/ingest",
    "REGENGINE_LIVE_API_KEY": "live-api-secret",
    "REGENGINE_LIVE_TENANT_ID": "live-tenant-secret",
}


# ---------------------------------------------------------------------------
# #105 -- scripts/live_trial.py: revert-to-mock on every exit path
# ---------------------------------------------------------------------------


class ScriptedLiveServer:
    """A fake demo backend that can be told to fail specific calls on cue.

    test_live_trial.py's FakeLiveTrialServer only ever succeeds, which is
    exactly why it could not have caught #105 -- everything it exercises is
    the happy path. This one can break the live step's own assertions, drop
    the connection mid-batch, or make the *revert* reset itself fail, so the
    failure/exception/loud-revert-failure paths are actually reachable.
    """

    def __init__(
        self,
        *,
        break_live_step_assertion: bool = False,
        drop_connection_on_live_step: bool = False,
        revert_status: int | None = None,
        drop_connection_on_revert: bool = False,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.reset_bodies: list[dict] = []
        self.current_mode = "mock"
        self.break_live_step_assertion = break_live_step_assertion
        self.drop_connection_on_live_step = drop_connection_on_live_step
        self.revert_status = revert_status
        self.drop_connection_on_revert = drop_connection_on_revert

    @property
    def reset_modes(self) -> list[str]:
        return [body["delivery"]["mode"] for body in self.reset_bodies]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/simulate/stop":
            return httpx.Response(200, json={"running": False})

        if path == "/api/simulate/reset":
            body = decode_json(request)
            self.reset_bodies.append(body)
            mode = body["delivery"]["mode"]
            self.current_mode = mode
            # A --confirm-live run resets exactly 3 times in order
            # (mock dry-run, live arm, mock revert): the revert is
            # whichever "mock" reset is not the first one.
            is_revert = mode == "mock" and self.reset_modes.count("mock") > 1
            if is_revert and self.drop_connection_on_revert:
                raise httpx.ConnectError("simulated connection drop on revert", request=request)
            if is_revert and self.revert_status is not None:
                return httpx.Response(self.revert_status, json={"detail": "revert rejected"})
            return httpx.Response(200, json={"status": "reset"})

        if path == "/api/simulate/step":
            if self.current_mode == "live":
                if self.drop_connection_on_live_step:
                    raise httpx.ConnectError("simulated connection drop on live step", request=request)
                if self.break_live_step_assertion:
                    # generated/delivery_mode intact so *this* call looks
                    # superficially fine; delivery_attempts is what
                    # run_one_live_batch actually asserts on, so this is
                    # what makes it raise LiveTrialFailure.
                    return httpx.Response(
                        200,
                        json={
                            "generated": 1,
                            "posted": 0,
                            "failed": 1,
                            "lot_codes": ["TLC-LIVE-TRIAL-001"],
                            "delivery_status": "failed",
                            "delivery_mode": "live",
                            "delivery_attempts": 0,
                            "response": {},
                            "error": "simulated live delivery rejection",
                        },
                    )
            return httpx.Response(
                200,
                json={
                    "generated": 1,
                    "posted": 1,
                    "failed": 0,
                    "lot_codes": ["TLC-LIVE-TRIAL-001"],
                    "delivery_status": "posted",
                    "delivery_mode": self.current_mode,
                    "delivery_attempts": 1,
                    "response": {},
                    "error": None,
                },
            )

        return httpx.Response(404, text=f"Unhandled path {path}")


def decode_json(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


def run_with_server(server: ScriptedLiveServer, argv: list[str], environ: dict[str, str]) -> int:
    with httpx.Client(
        base_url=environ["REGENGINE_REMOTE_BASE_URL"],
        transport=httpx.MockTransport(server.handle),
    ) as client:
        return main(argv, environ=environ, client=client)


def test_confirm_live_success_still_reverts_to_mock(capsys):
    """#105 acceptance criterion: the reset sequence is [mock, live, mock].

    tests/test_live_trial.py pins the pre-fix sequence ([mock, live]) and we
    do not own that file, so this is where the corrected sequence -- and the
    explicit revert confirmation line -- gets locked down.
    """
    server = ScriptedLiveServer()
    exit_code = run_with_server(server, ["--confirm-live"], LIVE_ENV)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert server.reset_modes == ["mock", "live", "mock"]
    assert "Live trial completed" in captured.out
    assert f"delivery reverted to mock for tenant {LIVE_ENV['REGENGINE_REMOTE_TENANT']}" in captured.out
    # The revert body must actually be mock with no live credentials left in it.
    assert server.reset_bodies[2]["delivery"] == {
        "mode": "mock",
        "endpoint": None,
        "api_key": None,
        "tenant_id": None,
    }
    assert "demo-password" not in captured.out
    assert "live-api-secret" not in captured.out
    assert "live-tenant-secret" not in captured.out


def test_revert_runs_when_live_batch_assertion_fails(capsys):
    """Revert must happen on the failure path, not just success (#105)."""
    server = ScriptedLiveServer(break_live_step_assertion=True)
    exit_code = run_with_server(server, ["--confirm-live"], LIVE_ENV)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Live trial failed" in captured.err
    assert "delivery attempts" in captured.err
    # Despite the trial failing, the tenant must still have been disarmed.
    assert server.reset_modes == ["mock", "live", "mock"]
    assert f"delivery reverted to mock for tenant {LIVE_ENV['REGENGINE_REMOTE_TENANT']}" in captured.out
    # A failed trial must not print the success banner.
    assert "Live trial completed" not in captured.out


def test_revert_runs_when_live_batch_raises_connection_error(capsys):
    """Revert must happen on a raw exception too, not only LiveTrialFailure (#105)."""
    server = ScriptedLiveServer(drop_connection_on_live_step=True)
    exit_code = run_with_server(server, ["--confirm-live"], LIVE_ENV)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Live trial failed: HTTP client error" in captured.err
    assert server.reset_modes == ["mock", "live", "mock"]
    assert f"delivery reverted to mock for tenant {LIVE_ENV['REGENGINE_REMOTE_TENANT']}" in captured.out


def test_dry_run_only_never_issues_a_revert_call(capsys):
    """No live arm ever happened, so there is nothing to revert (unchanged
    behavior -- this is what keeps test_live_trial.py's dry-run test, which
    pins reset_bodies == ["mock"], passing unmodified after the #105 fix."""
    server = ScriptedLiveServer()
    exit_code = run_with_server(server, ["--dry-run-only"], BASE_ENV)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert server.reset_modes == ["mock"]
    assert "delivery reverted to mock" not in captured.out


def test_failed_revert_after_trial_success_is_reported_not_swallowed(capsys):
    """#105: 'a revert that failed must be loud, because a silent one is the
    same as no revert.' The trial itself succeeds; only the final revert
    POST fails (non-200), and that alone must fail the whole run loudly."""
    server = ScriptedLiveServer(revert_status=500)
    exit_code = run_with_server(server, ["--confirm-live"], LIVE_ENV)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Live trial failed" in captured.err
    assert "CRITICAL" in captured.err
    assert LIVE_ENV["REGENGINE_REMOTE_TENANT"] in captured.err
    assert "may still be armed" in captured.err
    # The revert was attempted (3rd reset was sent) but did not succeed, so
    # the success confirmation line must never appear, and neither must the
    # trial's own success banner -- both would be false-green.
    assert server.reset_modes == ["mock", "live", "mock"]
    assert "delivery reverted to mock" not in captured.out
    assert "Live trial completed" not in captured.out


def test_failed_revert_via_connection_drop_is_reported_not_swallowed(capsys):
    """Same as above, but the revert POST fails via a dropped connection
    rather than a bad status code -- the exact gap the issue calls out:
    'a 4xx or a connection error passes silently' must no longer pass."""
    server = ScriptedLiveServer(drop_connection_on_revert=True)
    exit_code = run_with_server(server, ["--confirm-live"], LIVE_ENV)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Live trial failed" in captured.err
    assert "CRITICAL" in captured.err
    assert "may still be armed" in captured.err
    assert "delivery reverted to mock" not in captured.out


def test_failed_revert_after_trial_failure_reports_both_failures(capsys):
    """When the trial fails AND the revert also fails, the single printed
    line must not lose either fact -- the revert failure is the louder,
    more dangerous one, but silently dropping the original failure would
    also be a regression."""
    server = ScriptedLiveServer(break_live_step_assertion=True, revert_status=503)
    exit_code = run_with_server(server, ["--confirm-live"], LIVE_ENV)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "CRITICAL" in captured.err
    assert "may still be armed" in captured.err
    assert "trial itself had already failed" in captured.err
    assert "delivery attempts" in captured.err  # the original assertion failure


# ---------------------------------------------------------------------------
# #106 -- scripts/cutover_preflight.sh: probes must actually authenticate
# ---------------------------------------------------------------------------


class FakePreflightHandler(BaseHTTPRequestHandler):
    """Per-class-configured stand-in for the real app's routes.

    /api/healthz is unauthenticated by design (matching app/auth_middleware.py);
    every other path in the real proxy contract requires HTTP Basic Auth
    matching this instance's username/password, returning 401 application/json
    otherwise (matching app/auth.py's _unauthorized_response()).
    """

    username = ""
    password = ""
    build_commit_sha = "deadbeef"
    build_commit_source = "RAILWAY_GIT_COMMIT_SHA"
    trust_own_origin = True

    def log_message(self, format, *args):  # quiet test output
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/healthz":
            self._reply_healthz()
            return
        if not self._authenticated():
            self._reply_json(401, {"detail": "Authentication required"})
            return
        if path == "/api/simulate/status":
            self._reply_json(200, {"running": False, "delivery": {"mode": "mock"}})
        elif path == "/api/events":
            self._reply_json(200, {"events": []})
        elif path.startswith("/api/lineage/"):
            self._reply_json(200, {"traceability_lot_code": path.rsplit("/", 1)[-1], "records": []})
        elif path == "/api/mock/regengine/export/fda-request":
            self._reply_text(200, "Traceability Lot Code Description\n", "text/csv")
        elif path == "/api/mock/regengine/export/epcis":
            self._reply_json(200, {"epcisBody": {"eventList": []}}, content_type="application/ld+json")
        else:
            # Includes the two phantom /api/regengine/export/* paths #106
            # removed from PATHS -- if the script still probed them, this
            # 404 (matching on both sides) would fail the run under the
            # new "matching errors are not ok" rule and this test's happy
            # path would no longer reach PRE-FLIGHT PASSED.
            self._reply_json(404, {"detail": "not found"})

    def _authenticated(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
        except Exception:
            return False
        user, _, pw = decoded.partition(":")
        return user == self.username and pw == self.password

    def _reply_healthz(self) -> None:
        origin = self.headers.get("Origin")
        extra = {}
        if origin and self.trust_own_origin:
            extra["Access-Control-Allow-Origin"] = origin
        self._reply_json(
            200,
            {"ok": True, "build": {"commit_sha": self.build_commit_sha, "commit_source": self.build_commit_source}},
            extra_headers=extra,
        )

    def _reply_json(self, status, payload, extra_headers=None, content_type="application/json"):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _reply_text(self, status, text, content_type):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def fake_preflight_service(
    *,
    username: str,
    password: str,
    build_commit_sha: str = "deadbeef",
    build_commit_source: str = "RAILWAY_GIT_COMMIT_SHA",
    trust_own_origin: bool = True,
):
    """Start a 127.0.0.1-bound fake demo service for the duration of the `with`.

    A distinct handler subclass per call (rather than mutating shared class
    attributes) so two of these can run concurrently -- as an "old" and
    "new" pair -- with different credentials or build info.
    """
    handler_cls = type(
        "BoundFakePreflightHandler",
        (FakePreflightHandler,),
        {
            "username": username,
            "password": password,
            "build_commit_sha": build_commit_sha,
            "build_commit_source": build_commit_source,
            "trust_own_origin": trust_own_origin,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_preflight(old_url: str, new_url: str, *, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("REGENGINE_REMOTE_"):
            del env[key]
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(PREFLIGHT_SCRIPT), old_url, new_url],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cutover_preflight_shell_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", str(PREFLIGHT_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_preflight_refuses_green_without_credentials():
    """#106 core acceptance criterion: an unverifiable contract check must
    fail loudly, not print PRE-FLIGHT PASSED."""
    with fake_preflight_service(username="demo", password="demo-pw") as old_url, fake_preflight_service(
        username="demo", password="demo-pw"
    ) as new_url:
        result = run_preflight(old_url, new_url, env_overrides={})

    assert result.returncode != 0
    assert "PRE-FLIGHT PASSED" not in result.stdout
    assert "PRE-FLIGHT FAILED" in result.stdout
    assert "UNVERIFIABLE" in result.stdout
    assert "REGENGINE_REMOTE_USERNAME" in result.stdout


def test_preflight_passes_when_credentials_authenticate_the_real_contract():
    """With correct credentials supplied, every real path authenticates and
    matches, and the two phantom /api/regengine/export/* paths are no
    longer probed (see FakePreflightHandler's else-branch note)."""
    with fake_preflight_service(username="demo", password="demo-pw") as old_url, fake_preflight_service(
        username="demo", password="demo-pw"
    ) as new_url:
        result = run_preflight(
            old_url,
            new_url,
            env_overrides={
                "REGENGINE_REMOTE_USERNAME": "demo",
                "REGENGINE_REMOTE_PASSWORD": "demo-pw",
            },
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PRE-FLIGHT PASSED" in result.stdout
    assert "UNVERIFIABLE" not in result.stdout
    assert "  FAIL  " not in result.stdout
    assert "  DIFF  " not in result.stdout
    # 6 real paths in PATHS -- every one must have actually been checked.
    assert result.stdout.count("  ok    ") == 6


def test_preflight_fails_not_ok_when_both_services_401_with_wrong_credentials():
    """#106 acceptance criterion: matching error statuses on both sides must
    be reported as a failure, never as 'ok'. Simulated by supplying
    credentials that are wrong on both services -- the same shape as two
    genuinely different services both being unreachable/misconfigured."""
    with fake_preflight_service(username="demo", password="correct-pw") as old_url, fake_preflight_service(
        username="demo", password="correct-pw"
    ) as new_url:
        result = run_preflight(
            old_url,
            new_url,
            env_overrides={
                "REGENGINE_REMOTE_USERNAME": "demo",
                "REGENGINE_REMOTE_PASSWORD": "wrong-pw",
            },
        )

    assert result.returncode != 0
    assert "PRE-FLIGHT PASSED" not in result.stdout
    # /api/healthz is auth-exempt so it alone still authenticates ("ok");
    # every other path must be reported FAIL, not "ok", despite matching.
    assert "  FAIL  " in result.stdout
    assert "did not return 2xx with credentials" in result.stdout
    fail_lines = [line for line in result.stdout.splitlines() if line.startswith("  FAIL  ")]
    assert len(fail_lines) >= 5
    # The loop must still run to completion (set -uo pipefail, no -e):
    # every path gets probed and reported even after earlier failures.
    total_path_lines = sum(
        1 for line in result.stdout.splitlines() if line.startswith("  ok    ") or line.startswith("  FAIL  ") or line.startswith("  DIFF  ")
    )
    assert total_path_lines == 6


def test_preflight_reports_diff_not_ok_when_new_service_credentials_differ():
    """The scenario #106 says a green run currently hides: the new service
    has different Basic Auth credentials than the old one. With real
    authentication, old succeeds and new 401s -- a DIFF, not a match."""
    with fake_preflight_service(username="demo", password="shared-pw") as old_url, fake_preflight_service(
        username="demo", password="different-pw"
    ) as new_url:
        result = run_preflight(
            old_url,
            new_url,
            env_overrides={
                "REGENGINE_REMOTE_USERNAME": "demo",
                "REGENGINE_REMOTE_PASSWORD": "shared-pw",
            },
        )

    assert result.returncode != 0
    assert "PRE-FLIGHT PASSED" not in result.stdout
    assert "  DIFF  " in result.stdout
    # healthz needs no auth so both sides still match there ("ok"); every
    # other path must show as a DIFF once real credentials are in play.
    diff_lines = [line for line in result.stdout.splitlines() if line.startswith("  DIFF  ")]
    assert len(diff_lines) >= 5


def test_preflight_paths_only_reference_mounted_routes():
    """#106 acceptance criterion: PATHS contains only mounted routes. The
    two phantom paths (no /api/regengine router is ever mounted in
    app/main.py -- only mock_regengine's /api/mock/regengine prefix) must
    be gone, not merely bypassed."""
    script_text = PREFLIGHT_SCRIPT.read_text(encoding="utf-8")
    assert '"/api/regengine/export/fda-request"' not in script_text
    assert '"/api/regengine/export/epcis"' not in script_text
    assert '"/api/mock/regengine/export/fda-request"' in script_text
    assert '"/api/mock/regengine/export/epcis"' in script_text


def test_preflight_usage_message_unchanged():
    result = subprocess.run(
        ["bash", str(PREFLIGHT_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
