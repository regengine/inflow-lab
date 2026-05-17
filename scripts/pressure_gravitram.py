from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_SLOW_MS = 10_000.0
DEFAULT_HOT_MS = 30_000.0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    print(render_gravitram(report, slow_ms=args.slow_ms, hot_ms=args.hot_ms))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a terminal gravitram view from a pressure_trial JSON report."
    )
    parser.add_argument("report_json", help="Path to a pressure_trial --report-json artifact.")
    parser.add_argument(
        "--slow-ms",
        type=float,
        default=DEFAULT_SLOW_MS,
        help=f"Batch latency threshold marked as slow. Default: {DEFAULT_SLOW_MS:.0f}",
    )
    parser.add_argument(
        "--hot-ms",
        type=float,
        default=DEFAULT_HOT_MS,
        help=f"Batch latency threshold marked as hot. Default: {DEFAULT_HOT_MS:.0f}",
    )
    return parser.parse_args(argv)


def render_gravitram(
    report: dict[str, Any],
    *,
    slow_ms: float = DEFAULT_SLOW_MS,
    hot_ms: float = DEFAULT_HOT_MS,
) -> str:
    live = report.get("live", {})
    stages = live.get("stages", [])
    lines = [
        "Pressure Gravitram",
        (
            f"tenant={report.get('demo_tenant', '(unknown)')} "
            f"events={live.get('total_events_requested', 0)} "
            f"posted={live.get('total_posted', 0)} "
            f"failed={live.get('total_failed', 0)} "
            f"workers={report.get('workers', 0)}"
        ),
        f"legend: .=fast o=normal S=slow>={slow_ms:.0f}ms H=hot>={hot_ms:.0f}ms !=failed",
    ]

    all_batches: list[dict[str, Any]] = []
    for stage in stages:
        batches = stage.get("batches") or []
        all_batches.extend(batch | {"stage": stage.get("stage")} for batch in batches)
        lines.append(stage_line(stage, batches, slow_ms=slow_ms, hot_ms=hot_ms))

    lines.append(slowest_batch_line(all_batches))
    return "\n".join(lines)


def stage_line(
    stage: dict[str, Any],
    batches: list[dict[str, Any]],
    *,
    slow_ms: float,
    hot_ms: float,
) -> str:
    stage_id = stage.get("stage", "?")
    batch_size = stage.get("batch_size", "?")
    latency = stage.get("latency_ms", {})
    prefix = (
        f"S{stage_id} {batch_size}e "
        f"posted={stage.get('posted', 0)} "
        f"failed={stage.get('failed', 0)} "
        f"p95={float(latency.get('p95', 0.0)):.1f}ms "
        f"eps={float(stage.get('events_per_second', 0.0)):.2f} "
    )
    if not batches:
        return prefix + "[aggregate-only]"

    track = "".join(batch_symbol(batch, slow_ms=slow_ms, hot_ms=hot_ms) for batch in sorted_batches(batches))
    slow_count = sum(float(batch.get("latency_ms", 0.0)) >= slow_ms for batch in batches)
    failed_count = sum(int(batch.get("failed", 0)) > 0 for batch in batches)
    return prefix + f"[{track}] slow={slow_count}/{len(batches)} failed={failed_count}/{len(batches)}"


def batch_symbol(batch: dict[str, Any], *, slow_ms: float, hot_ms: float) -> str:
    if int(batch.get("failed", 0)) > 0:
        return "!"
    latency_ms = float(batch.get("latency_ms", 0.0))
    if latency_ms >= hot_ms:
        return "H"
    if latency_ms >= slow_ms:
        return "S"
    if latency_ms >= 1000.0:
        return "o"
    return "."


def slowest_batch_line(batches: list[dict[str, Any]]) -> str:
    if not batches:
        return "slowest batch: unavailable; report has no batch-level data"
    slowest = max(batches, key=lambda batch: float(batch.get("latency_ms", 0.0)))
    return (
        "slowest batch: "
        f"stage={slowest.get('stage')} "
        f"request={slowest.get('request')} "
        f"tenant={slowest.get('tenant')} "
        f"latency_ms={float(slowest.get('latency_ms', 0.0)):.1f} "
        f"posted={int(slowest.get('posted', 0))} "
        f"failed={int(slowest.get('failed', 0))}"
    )


def sorted_batches(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(batches, key=lambda batch: int(batch.get("request", 0)))


if __name__ == "__main__":
    raise SystemExit(main())
