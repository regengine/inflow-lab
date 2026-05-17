from __future__ import annotations

from scripts.pressure_gravitram import render_gravitram


def test_render_gravitram_shows_stage_track_and_slowest_batch() -> None:
    report = {
        "demo_tenant": "pressure-run",
        "workers": 2,
        "live": {
            "total_events_requested": 125,
            "total_posted": 100,
            "total_failed": 25,
            "total_requests": 3,
            "stages": [
                {
                    "stage": 1,
                    "batch_size": 25,
                    "requests": 3,
                    "posted": 50,
                    "failed": 25,
                    "events_per_second": 4.5,
                    "latency_ms": {"p50": 1200.0, "p95": 24000.0, "max": 24000.0},
                    "batches": [
                        {
                            "request": 1,
                            "tenant": "pressure-run-w1",
                            "generated": 25,
                            "posted": 25,
                            "failed": 0,
                            "latency_ms": 900.0,
                        },
                        {
                            "request": 2,
                            "tenant": "pressure-run-w2",
                            "generated": 25,
                            "posted": 25,
                            "failed": 0,
                            "latency_ms": 24000.0,
                        },
                        {
                            "request": 3,
                            "tenant": "pressure-run-w1",
                            "generated": 25,
                            "posted": 0,
                            "failed": 25,
                            "latency_ms": 1300.0,
                        },
                    ],
                }
            ],
        },
    }

    output = render_gravitram(report, slow_ms=10_000, hot_ms=30_000)

    assert "Pressure Gravitram" in output
    assert "events=125 posted=100 failed=25 workers=2" in output
    assert "S1 25e posted=50 failed=25 p95=24000.0ms eps=4.50 [.S!]" in output
    assert "slow=1/3 failed=1/3" in output
    assert "slowest batch: stage=1 request=2 tenant=pressure-run-w2 latency_ms=24000.0 posted=25 failed=0" in output


def test_render_gravitram_handles_aggregate_only_legacy_reports() -> None:
    report = {
        "demo_tenant": "legacy-run",
        "workers": 1,
        "live": {
            "total_events_requested": 10,
            "total_posted": 10,
            "total_failed": 0,
            "total_requests": 1,
            "stages": [
                {
                    "stage": 1,
                    "batch_size": 10,
                    "requests": 1,
                    "posted": 10,
                    "failed": 0,
                    "events_per_second": 1.0,
                    "latency_ms": {"p50": 1000.0, "p95": 1000.0, "max": 1000.0},
                }
            ],
        },
    }

    output = render_gravitram(report)

    assert "S1 10e posted=10 failed=0 p95=1000.0ms eps=1.00 [aggregate-only]" in output
    assert "slowest batch: unavailable; report has no batch-level data" in output
