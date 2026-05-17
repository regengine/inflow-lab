from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

try:
    from scripts.live_trial import (
        LiveTrialFailure,
        config_from_env,
        normalize_base_url,
        request_json,
        secret_values,
        stop_simulation,
        validate_requested_mode,
    )
except ModuleNotFoundError:
    from live_trial import (
        LiveTrialFailure,
        config_from_env,
        normalize_base_url,
        request_json,
        secret_values,
        stop_simulation,
        validate_requested_mode,
    )


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_SCENARIO = "fresh_cut_processor"
DEFAULT_STAGES = "3x1,3x10,3x25"


@dataclass(frozen=True)
class PressureStage:
    requests: int
    batch_size: int


@dataclass(frozen=True)
class PressureTrialConfig:
    base_url: str
    username: str
    password: str = field(repr=False)
    demo_tenant: str
    live_api_key: str = field(repr=False)
    live_tenant_id: str = field(repr=False)
    live_endpoint: str = "https://www.regengine.co/api/v1/webhooks/ingest"
    scenario: str = DEFAULT_SCENARIO
    seed: int = 204
    stages: tuple[PressureStage, ...] = (PressureStage(3, 1), PressureStage(3, 10), PressureStage(3, 25))
    workers: int = 1
    pause_ms: int = 0
    max_failures: int = 0
    max_p95_ms: float | None = None
    max_error_rate: float | None = None
    report_json: str | None = None
    watch: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def redact(self, value: str) -> str:
        redacted = value
        for secret in secret_values(self.password, self.live_api_key, self.live_tenant_id):
            redacted = redacted.replace(secret, "[redacted]")
        return redacted


@dataclass(frozen=True)
class StageResult:
    stage_index: int
    requests: int
    batch_size: int
    posted: int
    failed: int
    duration_seconds: float
    latencies_ms: tuple[float, ...]
    worker_tenants: tuple[str, ...]
    batches: tuple[BatchResult, ...]


@dataclass(frozen=True)
class BatchResult:
    tenant_id: str
    request_number: int
    generated: int
    posted: int
    failed: int
    latency_ms: float


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> int:
    try:
        args = parse_args(argv)
        validate_requested_mode(args)
        config = pressure_config_from_env_and_args(args, environ)
        summary = run_pressure_trial(config, confirm_live=args.confirm_live, client=client)
    except LiveTrialFailure as exc:
        print(f"Pressure trial failed: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"Pressure trial failed: HTTP client error: {exc}", file=sys.stderr)
        return 1

    if not args.confirm_live:
        write_report_if_requested(config, summary)
        print(
            "Pressure trial dry-run passed: "
            f"base_url={config.base_url}, "
            f"demo_tenant={config.demo_tenant}, "
            f"mock_posted={summary['mock_posted']}, "
            f"mock_failed={summary['mock_failed']}"
        )
        return 0

    write_report_if_requested(config, summary)
    print_live_summary(config, summary)
    return 0 if summary["total_failed"] <= config.max_failures else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a staged RegEngine pressure trial through an Inflow Lab instance. "
            "A mock dry-run is always performed first."
        )
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="After the mock dry-run succeeds, run the staged live pressure plan.",
    )
    parser.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Run the mock dry-run only and do not require live credentials.",
    )
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        help=f"Simulator scenario to use for dry-run and live stages. Default: {DEFAULT_SCENARIO}",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=204,
        help="Deterministic simulator seed. Default: 204",
    )
    parser.add_argument(
        "--stages",
        default=DEFAULT_STAGES,
        help=(
            "Comma-separated pressure stages in <requests>x<batch_size> form. "
            f"Default: {DEFAULT_STAGES}"
        ),
    )
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=0,
        help="Pause between live stages in milliseconds. Default: 0",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of tenant-scoped worker streams for live pressure. "
            "Values above 1 use tenant suffixes like <tenant>-w1. Default: 1"
        ),
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=0,
        help="Abort after this many failed live batches. Default: 0",
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=None,
        help="Abort after a live stage whose p95 request latency exceeds this value.",
    )
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=None,
        help="Abort after a live stage whose failed/generated event ratio exceeds this value.",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        help="Optional path to write a machine-readable JSON report.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Print one progress line as each live batch completes.",
    )
    parser.add_argument(
        "--live-endpoint",
        default=None,
        help="Optional override for the downstream RegEngine ingest URL.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout for calls to the Inflow Lab instance. Default: {DEFAULT_TIMEOUT_SECONDS:.0f}",
    )
    return parser.parse_args(argv)


def pressure_config_from_env_and_args(
    args: argparse.Namespace,
    environ: dict[str, str] | None = None,
) -> PressureTrialConfig:
    base = config_from_env(environ, require_live=args.confirm_live)
    if args.pause_ms < 0:
        raise LiveTrialFailure("--pause-ms must be >= 0")
    if args.workers < 1 or args.workers > 32:
        raise LiveTrialFailure("--workers must be between 1 and 32")
    if args.max_failures < 0:
        raise LiveTrialFailure("--max-failures must be >= 0")
    if args.max_p95_ms is not None and args.max_p95_ms <= 0:
        raise LiveTrialFailure("--max-p95-ms must be greater than 0")
    if args.max_error_rate is not None and not (0 <= args.max_error_rate <= 1):
        raise LiveTrialFailure("--max-error-rate must be between 0 and 1")
    if args.timeout_seconds <= 0:
        raise LiveTrialFailure("--timeout-seconds must be greater than 0")

    live_endpoint = args.live_endpoint or base.live_endpoint
    if args.confirm_live and not live_endpoint:
        raise LiveTrialFailure("Live endpoint is required for --confirm-live.")

    return PressureTrialConfig(
        base_url=base.base_url,
        username=base.username,
        password=base.password,
        demo_tenant=base.demo_tenant,
        live_endpoint=normalize_base_url(live_endpoint) if live_endpoint else "",
        live_api_key=base.live_api_key or "",
        live_tenant_id=base.live_tenant_id or "",
        scenario=args.scenario,
        seed=args.seed,
        stages=parse_stages(args.stages),
        workers=args.workers,
        pause_ms=args.pause_ms,
        max_failures=args.max_failures,
        max_p95_ms=args.max_p95_ms,
        max_error_rate=args.max_error_rate,
        report_json=args.report_json,
        watch=args.watch,
        timeout_seconds=args.timeout_seconds,
    )


def parse_stages(raw: str) -> tuple[PressureStage, ...]:
    stages: list[PressureStage] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if "x" not in token:
            raise LiveTrialFailure(
                f"Invalid stage {token!r}. Use <requests>x<batch_size>, for example 3x25."
            )
        requests_text, batch_size_text = token.split("x", 1)
        try:
            requests = int(requests_text)
            batch_size = int(batch_size_text)
        except ValueError as exc:
            raise LiveTrialFailure(
                f"Invalid stage {token!r}. Requests and batch size must be integers."
            ) from exc
        if requests < 1:
            raise LiveTrialFailure(f"Invalid stage {token!r}. Requests must be >= 1.")
        if batch_size < 1 or batch_size > 100:
            raise LiveTrialFailure(
                f"Invalid stage {token!r}. Batch size must be between 1 and 100."
            )
        stages.append(PressureStage(requests=requests, batch_size=batch_size))
    if not stages:
        raise LiveTrialFailure("At least one stage is required.")
    return tuple(stages)


def run_pressure_trial(
    config: PressureTrialConfig,
    *,
    confirm_live: bool,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            base_url=config.base_url,
            follow_redirects=True,
            timeout=config.timeout_seconds,
            verify=True,
        )

    try:
        mock_step = run_mock_dry_run(client, config)
        summary: dict[str, Any] = {
            "demo_tenant": config.demo_tenant,
            "mock_posted": mock_step.get("posted", 0),
            "mock_failed": mock_step.get("failed", 0),
            "mock_delivery_status": mock_step.get("delivery_status"),
        }
        if confirm_live:
            stage_results = run_live_stages(client, config)
            summary.update(
                {
                    "stage_results": stage_results,
                    "total_posted": sum(stage.posted for stage in stage_results),
                    "total_failed": sum(stage.failed for stage in stage_results),
                    "total_requests": sum(stage.requests for stage in stage_results),
                    "total_events_requested": sum(
                        stage.requests * stage.batch_size for stage in stage_results
                    ),
                }
            )
        return summary
    finally:
        tenants = worker_tenants(config) if confirm_live else (config.demo_tenant,)
        for tenant_id in tenants:
            stop_simulation(client, config_for_tenant(config, tenant_id))
        if owns_client:
            client.close()


def run_mock_dry_run(client: httpx.Client, config: PressureTrialConfig) -> dict[str, Any]:
    stop_simulation(client, config)
    request_json(
        client,
        config,
        "POST",
        "/api/simulate/reset",
        json={
            "source": "pressure-trial-dry-run",
            "scenario": config.scenario,
            "batch_size": 1,
            "seed": config.seed,
            "delivery": {"mode": "mock"},
        },
    )
    step = request_json(
        client,
        config,
        "POST",
        "/api/simulate/step",
        params={"batch_size": "1"},
    )
    if step.get("failed", 0) != 0:
        raise LiveTrialFailure(
            f"Mock dry-run failed: posted={step.get('posted', 0)} failed={step.get('failed', 0)}"
        )
    return step


def run_live_stages(client: httpx.Client, config: PressureTrialConfig) -> tuple[StageResult, ...]:
    tenant_configs = tuple(config_for_tenant(config, tenant_id) for tenant_id in worker_tenants(config))
    for tenant_config in tenant_configs:
        request_json(
            client,
            tenant_config,
            "POST",
            "/api/simulate/reset",
            json={
                "source": "pressure-trial",
                "scenario": config.scenario,
                "batch_size": config.stages[0].batch_size,
                "seed": config.seed,
                "delivery": {
                    "mode": "live",
                    "endpoint": config.live_endpoint,
                    "api_key": config.live_api_key,
                    "tenant_id": config.live_tenant_id,
                },
            },
        )

    total_failures = 0
    stage_results: list[StageResult] = []
    for index, stage in enumerate(config.stages, start=1):
        stage_started = time.perf_counter()
        batch_results = run_stage_requests(
            client=client,
            config=config,
            tenant_configs=tenant_configs,
            stage=stage,
            stage_index=index,
        )
        stage_result = StageResult(
            stage_index=index,
            requests=stage.requests,
            batch_size=stage.batch_size,
            posted=sum(result.posted for result in batch_results),
            failed=sum(result.failed for result in batch_results),
            duration_seconds=time.perf_counter() - stage_started,
            latencies_ms=tuple(result.latency_ms for result in batch_results),
            worker_tenants=tuple(tenant_config.demo_tenant for tenant_config in tenant_configs),
            batches=batch_results,
        )
        stage_results.append(stage_result)
        total_failures += stage_result.failed
        validate_stage_gates(config, stage_result, total_failures)
        if config.pause_ms and index < len(config.stages):
            time.sleep(config.pause_ms / 1000.0)
    return tuple(stage_results)


def run_stage_requests(
    *,
    client: httpx.Client,
    config: PressureTrialConfig,
    tenant_configs: tuple[PressureTrialConfig, ...],
    stage: PressureStage,
    stage_index: int,
) -> tuple[BatchResult, ...]:
    max_workers = min(config.workers, stage.requests)
    results: list[BatchResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                run_stage_request,
                client,
                tenant_configs[request_number % len(tenant_configs)],
                stage,
                stage_index,
                request_number + 1,
            )
            for request_number in range(stage.requests)
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if config.watch:
                print_batch_progress(stage_index, stage, result)
    return tuple(results)


def run_stage_request(
    client: httpx.Client,
    tenant_config: PressureTrialConfig,
    stage: PressureStage,
    stage_index: int,
    request_number: int,
) -> BatchResult:
    request_started = time.perf_counter()
    step = request_json(
        client,
        tenant_config,
        "POST",
        "/api/simulate/step",
        params={"batch_size": str(stage.batch_size)},
    )
    latency_ms = (time.perf_counter() - request_started) * 1000.0
    generated = int(step.get("generated", 0))
    if generated != stage.batch_size:
        raise LiveTrialFailure(
            f"Stage {stage_index} request {request_number}: expected generated="
            f"{stage.batch_size}, got {generated!r}"
        )
    if step.get("delivery_mode") != "live":
        raise LiveTrialFailure(
            f"Stage {stage_index} request {request_number}: expected delivery_mode='live', "
            f"got {step.get('delivery_mode')!r}"
        )
    return BatchResult(
        tenant_id=tenant_config.demo_tenant,
        request_number=request_number,
        generated=generated,
        posted=int(step.get("posted", 0)),
        failed=int(step.get("failed", 0)),
        latency_ms=latency_ms,
    )


def validate_stage_gates(
    config: PressureTrialConfig,
    stage: StageResult,
    total_failures: int,
) -> None:
    if total_failures > config.max_failures:
        raise LiveTrialFailure(
            f"Aborting after exceeding max failures: {total_failures} > {config.max_failures}"
        )
    if config.max_p95_ms is not None and percentile(stage.latencies_ms, 95) > config.max_p95_ms:
        raise LiveTrialFailure(
            f"Stage {stage.stage_index} p95 latency exceeded gate: "
            f"{percentile(stage.latencies_ms, 95):.1f}ms > {config.max_p95_ms:.1f}ms"
        )
    if config.max_error_rate is not None:
        attempted = stage.requests * stage.batch_size
        error_rate = stage.failed / attempted if attempted else 0.0
        if error_rate > config.max_error_rate:
            raise LiveTrialFailure(
                f"Stage {stage.stage_index} error rate exceeded gate: "
                f"{error_rate:.3f} > {config.max_error_rate:.3f}"
            )


def print_live_summary(config: PressureTrialConfig, summary: dict[str, Any]) -> None:
    stage_results: tuple[StageResult, ...] = summary["stage_results"]
    print(
        "Pressure trial completed: "
        f"base_url={config.base_url}, "
        f"demo_tenant={config.demo_tenant}, "
        f"live_endpoint={config.redact(config.live_endpoint)}, "
        f"total_requests={summary['total_requests']}, "
        f"total_events_requested={summary['total_events_requested']}, "
        f"total_posted={summary['total_posted']}, "
        f"total_failed={summary['total_failed']}, "
        f"workers={config.workers}"
    )
    for stage in stage_results:
        p50 = percentile(stage.latencies_ms, 50)
        p95 = percentile(stage.latencies_ms, 95)
        events_per_second = (
            (stage.requests * stage.batch_size) / stage.duration_seconds
            if stage.duration_seconds > 0
            else 0.0
        )
        print(
            f"stage={stage.stage_index} "
            f"requests={stage.requests} "
            f"batch_size={stage.batch_size} "
            f"posted={stage.posted} "
            f"failed={stage.failed} "
            f"duration_s={stage.duration_seconds:.2f} "
            f"eps={events_per_second:.2f} "
            f"p50_ms={p50:.1f} "
            f"p95_ms={p95:.1f} "
            f"workers={len(stage.worker_tenants)}"
        )


def print_batch_progress(stage_index: int, stage: PressureStage, result: BatchResult) -> None:
    print(
        "batch "
        f"stage={stage_index} "
        f"request={result.request_number}/{stage.requests} "
        f"tenant={result.tenant_id} "
        f"batch_size={stage.batch_size} "
        f"generated={result.generated} "
        f"posted={result.posted} "
        f"failed={result.failed} "
        f"latency_ms={result.latency_ms:.1f}",
        flush=True,
    )


def percentile(values: tuple[float, ...], p: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((p / 100) * (len(ordered) - 1))))
    return ordered[index]


def worker_tenants(config: PressureTrialConfig) -> tuple[str, ...]:
    if config.workers == 1:
        return (config.demo_tenant,)

    tenants = tuple(f"{config.demo_tenant}-w{index}" for index in range(1, config.workers + 1))
    too_long = [tenant_id for tenant_id in tenants if len(tenant_id) > 64]
    if too_long:
        raise LiveTrialFailure(
            "Worker tenant ids must be 64 characters or fewer; shorten REGENGINE_REMOTE_TENANT."
        )
    return tenants


def config_for_tenant(config: PressureTrialConfig, tenant_id: str) -> PressureTrialConfig:
    return replace(config, demo_tenant=tenant_id)


def write_report_if_requested(config: PressureTrialConfig, summary: dict[str, Any]) -> None:
    if not config.report_json:
        return
    report_path = Path(config.report_json)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report_payload(config, summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def report_payload(config: PressureTrialConfig, summary: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "base_url": config.base_url,
        "demo_tenant": config.demo_tenant,
        "scenario": config.scenario,
        "seed": config.seed,
        "workers": config.workers,
        "mock": {
            "posted": summary.get("mock_posted", 0),
            "failed": summary.get("mock_failed", 0),
            "delivery_status": summary.get("mock_delivery_status"),
        },
    }
    if "stage_results" in summary:
        payload["live"] = {
            "endpoint": config.redact(config.live_endpoint),
            "total_requests": summary["total_requests"],
            "total_events_requested": summary["total_events_requested"],
            "total_posted": summary["total_posted"],
            "total_failed": summary["total_failed"],
            "stages": [stage_payload(stage) for stage in summary["stage_results"]],
        }
    return payload


def stage_payload(stage: StageResult) -> dict[str, Any]:
    attempted = stage.requests * stage.batch_size
    return {
        "stage": stage.stage_index,
        "requests": stage.requests,
        "batch_size": stage.batch_size,
        "events_requested": attempted,
        "posted": stage.posted,
        "failed": stage.failed,
        "error_rate": stage.failed / attempted if attempted else 0.0,
        "duration_seconds": round(stage.duration_seconds, 3),
        "events_per_second": round(attempted / stage.duration_seconds, 3)
        if stage.duration_seconds > 0
        else 0.0,
        "latency_ms": {
            "p50": round(percentile(stage.latencies_ms, 50), 3),
            "p95": round(percentile(stage.latencies_ms, 95), 3),
            "max": round(max(stage.latencies_ms), 3) if stage.latencies_ms else 0.0,
        },
        "worker_tenants": list(stage.worker_tenants),
        "batches": [batch_payload(batch) for batch in sorted(stage.batches, key=lambda item: item.request_number)],
    }


def batch_payload(batch: BatchResult) -> dict[str, Any]:
    return {
        "request": batch.request_number,
        "tenant": batch.tenant_id,
        "generated": batch.generated,
        "posted": batch.posted,
        "failed": batch.failed,
        "latency_ms": round(batch.latency_ms, 3),
    }


if __name__ == "__main__":
    raise SystemExit(main())
