"""End-to-end customer journey: Meridian Fresh Ops -> RegEngine.

Replays the full experience a real customer has hooking their factory
software up to RegEngine, against a real RegEngine stack:

1.  Reach RegEngine (health probe).
2.  Onboard: provision a tenant and an API key through the admin API
    (local mode only — in the wild this is done for you by RegEngine).
3.  Activate billing: seed the subscription gate's Redis status
    (local mode only — deployed tenants already have billing state).
4.  Test the connection with the provisioned credentials.
5.  Run the factory: generate canonical CTE batches with the same engine
    and live client the operator console uses, and ingest them.
6.  Feel the friction (optional): a KDE-rejected event and an
    idempotency replay, exactly as a live integrator hits them.
7.  Verify the evidence on the RegEngine side: recent events, hash-chain
    verification, and an FDA export.
8.  Tear down: delete the tenant and API key step 2 provisioned, so a
    stack used for repeated runs does not accumulate one of each per run
    (local mode only, and from a finally block so it also runs when an
    earlier step fails).

Local mode (default):

    export REGENGINE_ADMIN_KEY=...           # ADMIN_MASTER_KEY of the stack
    export REGENGINE_BASE_URL=http://localhost:8000
    export REGENGINE_REDIS_URL=redis://localhost:6379/0
    uv run python scripts/customer_journey.py --local

Deployed mode (never provisions, never touches Redis, small batch):

    export REGENGINE_LIVE_ENDPOINT={live_ingest_endpoint}
    export REGENGINE_LIVE_API_KEY=...
    export REGENGINE_LIVE_TENANT_ID=...
    uv run python scripts/customer_journey.py --confirm-live

The script refuses to run without an explicit mode flag, mirroring
scripts/live_trial.py: there is no default that touches a live system.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import ssl
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import LegitFlowEngine  # noqa: E402
from app.regengine_client import (  # noqa: E402
    DEFAULT_LIVE_INGEST_ENDPOINT,
    LiveRegEngineClient,
    LiveRegEngineDeliveryError,
)
from app.schemas.ingestion import IngestPayload  # noqa: E402
from app.schemas.simulation import DeliveryConfig, SimulationConfig  # noqa: E402
from app.scenarios import ScenarioId  # noqa: E402


# #155: the deployed-mode example above is rendered from the one live
# ingest URL the repo keeps, rather than repeating the literal here, so
# `--help` cannot drift from what the live client actually posts to.
# str.replace rather than str.format: the docstring is free-form prose and
# a brace someone adds to it later must not become a KeyError at import.
_USAGE = (__doc__ or "").replace("{live_ingest_endpoint}", DEFAULT_LIVE_INGEST_ENDPOINT)

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
JOURNEY_SCOPES = ["webhooks.ingest", "webhooks.read", "fda.export"]
JOURNEY_SOURCE = "meridian-ops-journey"


@dataclass(slots=True)
class JourneyReport:
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((name, ok, detail))
        marker = "PASS" if ok else "FAIL"
        line = f"[{marker}] {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    @property
    def failed(self) -> bool:
        return any(not ok for _, ok, _ in self.steps)


def parse_redis_url(url: str) -> tuple[str, int, int, str | None]:
    """Return (host, port, db, password) for a redis:// URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError(f"Unsupported Redis URL scheme: {parsed.scheme}")
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    db = 0
    path = (parsed.path or "").lstrip("/")
    if path:
        db = int(path)
    return host, port, db, parsed.password


def resp_command(*parts: str) -> bytes:
    """Encode a Redis command in RESP wire format."""
    encoded = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        raw = part.encode("utf-8")
        encoded.append(f"${len(raw)}\r\n".encode() + raw + b"\r\n")
    return b"".join(encoded)


# Human names for the RESP type bytes this client understands, used only to
# make a wrong-type failure legible in the journey report.
_RESP_TYPE_NAMES = {b"+": "simple string", b":": "integer", b"$": "bulk string"}


def _read_reply(conn_file: Any, label: str) -> tuple[bytes, bytes | None]:
    """Read one RESP reply, or raise explaining why there wasn't one.

    The old version of this checked only for a leading b"-" and called
    everything else success (#109). Two things that are not error replies
    reached that check and passed it: an empty read, which is what a peer
    that closed the connection returns, and arbitrary bytes such as a TLS
    alert from a server expecting a handshake. Both made the journey print
    PASS for the billing seed and then fail every subsequent ingest at the
    subscription gate -- the operator staring at a green line for the step
    that actually broke.
    """
    line = conn_file.readline()
    if not line:
        raise RuntimeError(
            f"Redis closed the connection without replying to {label}. "
            "An empty read is not a success: check the server is reachable, "
            "and that a TLS endpoint is addressed as rediss:// rather than redis://."
        )
    kind, body = line[:1], line[1:].rstrip(b"\r\n")
    if kind == b"-":
        raise RuntimeError(f"Redis rejected {label}: {body.decode('utf-8', 'replace')}")
    if kind == b"$":
        try:
            length = int(body)
        except ValueError as exc:
            raise RuntimeError(
                f"Redis sent a malformed bulk-string header for {label}: {line[:64]!r}"
            ) from exc
        if length < 0:
            return kind, None
        chunk = conn_file.read(length + 2)
        if len(chunk) < length:
            raise RuntimeError(f"Redis sent a truncated reply to {label}.")
        return kind, chunk[:length]
    if kind in (b"+", b":"):
        return kind, body
    raise RuntimeError(
        f"Redis replied to {label} with an unrecognized RESP type {kind!r}: {line[:64]!r}. "
        "Bytes like these are what a TLS handshake alert, or a non-Redis service "
        "on this port, looks like from here."
    )


def _expect_reply(
    conn_file: Any,
    label: str,
    kind: bytes,
    value: bytes | None = None,
) -> bytes | None:
    actual_kind, actual_value = _read_reply(conn_file, label)
    if actual_kind != kind:
        raise RuntimeError(
            f"{label}: expected a RESP {_RESP_TYPE_NAMES[kind]} reply, got "
            f"{_RESP_TYPE_NAMES.get(actual_kind, repr(actual_kind))} {actual_value!r}."
        )
    if value is not None and actual_value != value:
        raise RuntimeError(
            f"{label}: expected {value!r}, got {actual_value!r}."
        )
    return actual_value


def seed_billing_status(redis_url: str, tenant_id: str, status: str = "trialing") -> None:
    """HSET billing:tenant:{tenant_id} status <status> via a raw socket.

    Uses a minimal RESP client so the journey script needs no Redis
    dependency. Every reply is checked for its expected RESP type, and the
    value is read back with HGET before this returns -- so the PASS line the
    caller prints means the key is actually set, not merely that no error
    reply arrived.

    rediss:// URLs negotiate TLS before anything is written. parse_redis_url
    has always accepted that scheme, but the socket was never wrapped, so
    the AUTH password went out in plaintext and the handshake then failed
    (#109).
    """
    scheme = (urlparse(redis_url).scheme or "").lower()
    host, port, db, password = parse_redis_url(redis_url)
    conn: Any = socket.create_connection((host, port), timeout=5)
    try:
        if scheme == "rediss":
            # Before any sendall below, so a failed handshake cannot leak the
            # credential the next line would have written.
            conn = ssl.create_default_context().wrap_socket(conn, server_hostname=host)
        conn_file = conn.makefile("rb")

        def send(*parts: str) -> None:
            conn.sendall(resp_command(*parts))

        if password:
            send("AUTH", password)
            _expect_reply(conn_file, "AUTH", b"+", b"OK")
        if db:
            send("SELECT", str(db))
            _expect_reply(conn_file, f"SELECT {db}", b"+", b"OK")

        key = f"billing:tenant:{tenant_id}"
        send("HSET", key, "status", status)
        # HSET answers 1 for a new field and 0 for an overwritten one; both
        # are successful writes, so only the integer type is asserted here.
        _expect_reply(conn_file, "HSET", b":")

        send("HGET", key, "status")
        stored = _expect_reply(conn_file, "HGET", b"$")
        if stored != status.encode("utf-8"):
            raise RuntimeError(
                f"Redis stored {stored!r} for {key} status, expected {status!r}."
            )
    finally:
        conn.close()


def record_billing_seed(report: JourneyReport, redis_url: str, tenant_id: str) -> None:
    """Seed the subscription gate's billing status and report the outcome.

    Split out of run_journey so the failure paths seed_billing_status now
    raises on (#109) can be exercised end-to-end: what matters is not only
    that it raises, but that the journey records FAIL rather than PASS.
    """
    try:
        seed_billing_status(redis_url, tenant_id, "trialing")
    except Exception as exc:  # noqa: BLE001 - the gate will 402/503 without it
        report.record(
            "Activate billing (Redis seed)",
            False,
            f"{exc} — without it the subscription gate returns 402/503. "
            "Seed manually: redis-cli HSET billing:tenant:<id> status trialing, "
            "or set SUBSCRIPTION_GATE_FAIL_OPEN=true on the stack.",
        )
        return
    report.record("Activate billing (Redis seed)", True, "status=trialing")


async def provision_tenant_and_key(
    client: httpx.AsyncClient, base_url: str, admin_key: str
) -> tuple[str, str, str | None]:
    """Create a journey tenant and a key scoped to it.

    Returns (tenant_id, api_key, key_id). The key id is what
    deprovision_tenant_and_key needs to delete the key on its own; it is
    optional because deleting the tenant is expected to take its keys with
    it, and not every admin API returns an id for a freshly minted key.
    """
    headers = {"X-Admin-Key": admin_key}
    tenant_response = await client.post(
        f"{base_url}/v1/admin/tenants",
        headers=headers,
        json={"name": f"Meridian Fresh Foods (journey {uuid.uuid4().hex[:8]})"},
    )
    tenant_response.raise_for_status()
    tenant_id = tenant_response.json()["tenant_id"]

    key_response = await client.post(
        f"{base_url}/v1/admin/keys",
        headers=headers,
        json={
            "name": "meridian-ops journey key",
            "tenant_id": tenant_id,
            "scopes": JOURNEY_SCOPES,
        },
    )
    key_response.raise_for_status()
    key_payload = key_response.json()
    api_key = key_payload.get("api_key") or key_payload.get("key")
    if not api_key:
        raise RuntimeError("Admin key creation response did not include the raw API key")
    key_id = key_payload.get("key_id") or key_payload.get("id")
    return tenant_id, api_key, str(key_id) if key_id else None


async def deprovision_tenant_and_key(
    client: httpx.AsyncClient,
    base_url: str,
    admin_key: str,
    tenant_id: str,
    key_id: str | None,
    report: JourneyReport,
) -> None:
    """Delete what provision_tenant_and_key created, and say whether it worked.

    Every --local run minted a fresh "Meridian Fresh Foods (journey ...)"
    tenant and an API key and left both behind (#190), so a stack used for
    iterating on the onboarding flow accumulated one of each per run --
    surfacing later only as a cluttered tenant picker or a mysteriously
    large local database. The two sibling release scripts both remove the
    tenant footprint they create from a finally block; this is the same
    contract for the one thing this script provisions.

    The outcome is recorded as a journey step rather than logged and
    swallowed. If the admin API has no delete route, that is a real leftover
    tenant and the operator needs to see it, with the id needed to remove it
    by hand.
    """
    headers = {"X-Admin-Key": admin_key}
    # 204/200/202 are all "done"; 404 means it is already gone, which is the
    # state we wanted either way.
    gone = {200, 202, 204, 404}
    problems: list[str] = []

    if key_id:
        try:
            response = await client.delete(f"{base_url}/v1/admin/keys/{key_id}", headers=headers)
            if response.status_code not in gone:
                problems.append(f"key {key_id}: HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            problems.append(f"key {key_id}: {exc.__class__.__name__}: {exc}")

    try:
        response = await client.delete(f"{base_url}/v1/admin/tenants/{tenant_id}", headers=headers)
        if response.status_code not in gone:
            problems.append(f"tenant {tenant_id}: HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        problems.append(f"tenant {tenant_id}: {exc.__class__.__name__}: {exc}")

    if problems:
        report.record(
            "Teardown: journey tenant + key removed",
            False,
            "; ".join(problems)
            + f" — remove tenant {tenant_id} by hand, or the stack keeps one "
            "journey tenant per run.",
        )
        return
    report.record("Teardown: journey tenant + key removed", True, f"tenant {tenant_id}")


def build_config(endpoint: str, api_key: str, tenant_id: str) -> SimulationConfig:
    return SimulationConfig(
        source=JOURNEY_SOURCE,
        scenario=ScenarioId.FRESH_CUT_PROCESSOR,
        delivery=DeliveryConfig(
            mode="live",
            endpoint=endpoint,
            api_key=api_key,
            tenant_id=tenant_id,
        ),
    )


def generate_batch(engine: LegitFlowEngine, size: int) -> IngestPayload:
    events = []
    for _ in range(size):
        event, _parents = engine.next_event()
        events.append(event)
    return IngestPayload(source=JOURNEY_SOURCE, events=events)


async def run_friction_demos(
    live_client: LiveRegEngineClient,
    config: SimulationConfig,
    engine: LegitFlowEngine,
    report: JourneyReport,
) -> None:
    # 1. KDE rejection: strip the canonical reference_document so RegEngine's
    #    strict validator rejects the event — the most common integration bug.
    event, _ = engine.next_event()
    broken = event.model_copy(deep=True)
    broken.kdes.pop("reference_document", None)
    payload = IngestPayload(source=JOURNEY_SOURCE, events=[broken])
    try:
        result = await live_client.ingest(payload, config, idempotency_key=uuid.uuid4().hex)
        rejected = result.response.get("rejected", 0)
        event_errors = (result.response.get("events") or [{}])[0].get("errors") or []
        ok = rejected == 1 and any("reference_document" in err for err in event_errors)
        report.record(
            "Friction: KDE rejection",
            ok,
            f"rejected={rejected} errors={event_errors[:1]}",
        )
    except LiveRegEngineDeliveryError as exc:
        report.record("Friction: KDE rejection", False, str(exc))

    # 2. Idempotency replay: same body, same Idempotency-Key -> RegEngine
    #    must return its cached response instead of double-ingesting.
    replay_key = uuid.uuid4().hex
    payload = generate_batch(engine, 2)
    try:
        first = await live_client.ingest(payload, config, idempotency_key=replay_key)
        second = await live_client.ingest(payload, config, idempotency_key=replay_key)
        first_ids = [item.get("event_id") for item in first.response.get("events", [])]
        second_ids = [item.get("event_id") for item in second.response.get("events", [])]
        ok = bool(first_ids) and first_ids == second_ids
        report.record(
            "Friction: idempotent replay",
            ok,
            "cached response replayed" if ok else f"event ids diverged: {first_ids} vs {second_ids}",
        )
    except LiveRegEngineDeliveryError as exc:
        report.record("Friction: idempotent replay", False, str(exc))


async def verify_evidence(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    tenant_id: str,
    report: JourneyReport,
    expected_min_events: int,
) -> None:
    headers = {"X-RegEngine-API-Key": api_key, "X-Tenant-ID": tenant_id}

    recent = await client.get(
        f"{base_url}/api/v1/webhooks/recent",
        headers=headers,
        params={"tenant_id": tenant_id, "limit": 10},
    )
    recent_ok = recent.status_code == 200
    recent_count = len((recent.json() or {}).get("events", [])) if recent_ok else 0
    report.record(
        "Verify: recent events visible",
        recent_ok and recent_count > 0,
        f"HTTP {recent.status_code}, {recent_count} event(s) returned",
    )

    chain = await client.get(
        f"{base_url}/api/v1/webhooks/chain/verify",
        headers=headers,
        params={"tenant_id": tenant_id},
    )
    chain_body: dict[str, Any] = chain.json() if chain.status_code == 200 else {}
    chain_ok = chain.status_code == 200 and bool(chain_body.get("chain_valid"))
    report.record(
        "Verify: hash chain valid",
        chain_ok,
        f"HTTP {chain.status_code}, length={chain_body.get('chain_length')}",
    )

    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    export = await client.get(
        f"{base_url}/api/v1/fda/export/all",
        headers=headers,
        params={
            "tenant_id": tenant_id,
            "start_date": (today - timedelta(days=2)).isoformat(),
            "end_date": today.isoformat(),
        },
    )
    report.record(
        "Verify: FDA export",
        export.status_code == 200,
        f"HTTP {export.status_code}"
        + (f", export hash {export.headers.get('X-Export-Hash', 'n/a')}" if export.status_code == 200 else f", body: {export.text[:200]}"),
    )

    if expected_min_events and recent_ok and recent_count < min(expected_min_events, 10):
        report.record(
            "Verify: expected event volume",
            False,
            f"expected at least {min(expected_min_events, 10)} of the ingested events in /recent",
        )


async def run_journey(
    args: argparse.Namespace,
    *,
    client: httpx.AsyncClient | None = None,
) -> int:
    report = JourneyReport()
    # Set the moment provisioning succeeds, so the teardown below knows a
    # tenant exists even if the very next step raises. Stays None for
    # --confirm-live, which provisions nothing.
    provisioned: tuple[str, str | None] | None = None
    admin_key = ""

    if args.confirm_live:
        endpoint = os.environ.get("REGENGINE_LIVE_ENDPOINT", "").strip()
        api_key = os.environ.get("REGENGINE_LIVE_API_KEY", "").strip()
        tenant_id = os.environ.get("REGENGINE_LIVE_TENANT_ID", "").strip()
        missing = [
            name
            for name, value in {
                "REGENGINE_LIVE_ENDPOINT": endpoint,
                "REGENGINE_LIVE_API_KEY": api_key,
                "REGENGINE_LIVE_TENANT_ID": tenant_id,
            }.items()
            if not value
        ]
        if missing:
            print(f"Missing required env vars for --confirm-live: {', '.join(sorted(missing))}")
            return 2
        base_url = endpoint.split("/api/v1/")[0]
        batches, batch_size = 1, min(args.batch_size, 3)
        # Deployed runs stay minimal, mirroring live_trial.py: one small
        # batch, no deliberate rejections or replays against production.
        args.friction = False
    else:
        base_url = os.environ.get("REGENGINE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        endpoint = f"{base_url}/api/v1/webhooks/ingest"
        admin_key = os.environ.get("REGENGINE_ADMIN_KEY", "").strip()
        if not admin_key:
            print("REGENGINE_ADMIN_KEY is required for --local (the stack's ADMIN_MASTER_KEY).")
            return 2
        batches, batch_size = args.batches, args.batch_size

    # A caller-supplied client belongs to the caller, so it is wrapped in
    # nullcontext instead of being closed on the way out of this block.
    client_context = (
        contextlib.nullcontext(client)
        if client is not None
        else httpx.AsyncClient(timeout=30.0)
    )
    async with client_context as client:
        # 1. Reach RegEngine.
        try:
            health = await client.get(f"{base_url}/health")
            report.record("Reach RegEngine", health.status_code == 200, f"HTTP {health.status_code} from {base_url}/health")
        except httpx.HTTPError as exc:
            report.record("Reach RegEngine", False, f"{exc.__class__.__name__}: {base_url} unreachable")
            return 1

        # 2-3. Onboarding (local only).
        if not args.confirm_live:
            try:
                tenant_id, api_key, key_id = await provision_tenant_and_key(
                    client, base_url, admin_key
                )
                provisioned = (tenant_id, key_id)
                report.record("Onboard: tenant + API key provisioned", True, f"tenant {tenant_id}")
            except Exception as exc:  # noqa: BLE001 - report and stop
                report.record("Onboard: tenant + API key provisioned", False, str(exc))
                return 1

            record_billing_seed(
                report,
                os.environ.get("REGENGINE_REDIS_URL", DEFAULT_REDIS_URL),
                tenant_id,
            )

        try:
            # 4. Test the connection like the console does.
            live_client = LiveRegEngineClient()
            config = build_config(endpoint, api_key, tenant_id)
            check = await live_client.check_connection(config)
            report.record(
                "Test connection",
                check.verdict == "connected",
                f"{check.verdict}: {check.detail}",
            )

            # 5. Run the factory. Clamp the engine's demo clock to "now": the
            # webhook validator allows +24h, but RegEngine's canonical storage
            # layer only tolerates small clock skew — future-dated events pass
            # validation and then fail persistence with a per-event storage
            # error, which is not the journey we want to demonstrate.
            os.environ.setdefault("REGENGINE_SIM_MAX_FUTURE_HOURS", "0")
            engine = LegitFlowEngine()
            engine.reset(args.seed, scenario=ScenarioId.FRESH_CUT_PROCESSOR, scale=args.scale)
            ingested = 0
            for index in range(batches):
                payload = generate_batch(engine, batch_size)
                try:
                    result = await live_client.ingest(payload, config, idempotency_key=uuid.uuid4().hex)
                    accepted = result.response.get("accepted", 0)
                    rejected = result.response.get("rejected", 0)
                    ingested += accepted
                    report.record(
                        f"Ingest batch {index + 1}/{batches}",
                        rejected == 0 and accepted == len(payload.events),
                        f"accepted={accepted} rejected={rejected}",
                    )
                    if rejected:
                        for item in result.response.get("events", []):
                            if item.get("status") == "rejected":
                                print(f"       rejected {item.get('traceability_lot_code')}: {item.get('errors')}")
                except LiveRegEngineDeliveryError as exc:
                    report.record(f"Ingest batch {index + 1}/{batches}", False, str(exc))

            # 6. Friction demos.
            if args.friction:
                await run_friction_demos(live_client, config, engine, report)

            # 7. Verify the evidence RegEngine now holds.
            await verify_evidence(client, base_url, api_key, tenant_id, report, expected_min_events=ingested)
        finally:
            # Runs on the success path, on any exception mid-journey, and
            # on a KeyboardInterrupt. The tenant exists from the moment
            # provisioning returned, so removing it cannot be conditional
            # on the rest of the journey working (#190). --confirm-live
            # provisions nothing, so provisioned stays None and this is a
            # no-op there.
            if provisioned is not None:
                await deprovision_tenant_and_key(
                    client, base_url, admin_key, provisioned[0], provisioned[1], report
                )

    print()
    passed = sum(1 for _, ok, _ in report.steps if ok)
    print(f"Customer journey: {passed}/{len(report.steps)} steps passed.")
    return 1 if report.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=_USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true", help="Run against a local RegEngine stack; provisions tenant + key + billing.")
    mode.add_argument("--confirm-live", action="store_true", help="Run against a deployed RegEngine with pre-provisioned credentials. Small batch, no provisioning, no Redis.")
    parser.add_argument("--batches", type=int, default=3, help="Number of ingest batches (local mode; deployed is always 1).")
    parser.add_argument("--batch-size", type=int, default=5, help="Events per batch (capped at 3 in deployed mode).")
    parser.add_argument("--seed", type=int, default=204, help="Engine seed for reproducible journeys.")
    parser.add_argument(
        "--scale",
        choices=["small", "midsize", "enterprise"],
        default="midsize",
        help="Operation size: single-farm small producer, the default mid-size operation, or a multi-site enterprise network.",
    )
    parser.add_argument("--no-friction", dest="friction", action="store_false", help="Skip the KDE-rejection and idempotency-replay demos.")
    args = parser.parse_args()
    return asyncio.run(run_journey(args))


if __name__ == "__main__":
    raise SystemExit(main())
