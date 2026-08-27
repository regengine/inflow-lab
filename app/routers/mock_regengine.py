from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..epcis_export import epcis_filename, render_epcis_document
from ..mock_service import (
    EVENT_AGE_MODE_WARN,
    MAX_BATCH_EVENTS,
    MAX_INGEST_BODY_BYTES,
    MockRegEngineHTTPError,
    event_age_mode,
    max_event_age_days,
    parse_ingest_payload,
    validate_friction_codes,
)
from ..fda_export import (
    FDA_EXPORT_PRESETS,
    apply_fda_export_preset,
    export_filename,
    list_fda_export_preset_summaries,
    render_fda_request_csv,
)
from ..schemas.domain import FDAExportPreset, StoredEventRecord
from ..schemas.exports import (
    EpcisDocumentResponse,
    FDAExportPresetListResponse,
    FDAExportPresetSummary,
)
from ..schemas.ingestion import IngestPayload, MockIngestResponse


router = APIRouter(prefix="/api/mock/regengine", tags=["Mock RegEngine"])

# Exports walk whole lot graphs or whole date ranges, so like /api/lineage
# they are bounded rather than unbounded. The cap is deliberately far more
# generous than the lineage view: an FDA request export is a compliance
# artifact, and the failure mode we care about is an operator handing a
# regulator a file that quietly stopped short. So the default only bites on
# genuinely huge exports, and when it does bite the truncation is impossible
# to miss - see `_export_bounds_headers` and the banner/summary below.
EXPORT_DEFAULT_LIMIT = 10_000
EXPORT_MAX_LIMIT = 100_000

TRUNCATION_BANNER_PREFIX = "# PARTIAL EXPORT - NOT A COMPLETE RECORD SET"


def _inlined_schema(model: type[BaseModel]) -> dict[str, Any]:
    """``model``'s JSON schema with every ``$defs`` reference expanded in place.

    ``openapi_extra`` schemas are spliced into the document verbatim, and a
    ``$ref`` inside one resolves against the OPENAPI DOCUMENT root, not against
    the embedded schema — so a `#/$defs/RegEngineEvent` produced by
    ``model_json_schema()`` would dangle, and `#/components/schemas/...` would
    dangle too now that no route registers `IngestPayload` as a component.
    Inlining sidesteps both. The models here are shallow and non-recursive.
    """
    schema = model.model_json_schema()
    defs: dict[str, Any] = schema.pop("$defs", {})

    def expand(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                resolved = dict(defs[ref.removeprefix("#/$defs/")])
                resolved.update({k: v for k, v in node.items() if k != "$ref"})
                return expand(resolved)
            return {key: expand(value) for key, value in node.items()}
        if isinstance(node, list):
            return [expand(item) for item in node]
        return node

    return expand(schema)


#: Declared by hand because the handler no longer takes a parsed body
#: parameter — see the docstring below. Without this the route would publish no
#: request body at all and the generated client and docs would lose the payload
#: shape entirely.
_INGEST_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": _inlined_schema(IngestPayload)}},
    }
}


@router.post("/ingest", response_model=MockIngestResponse, openapi_extra=_INGEST_REQUEST_BODY)
async def mock_regengine_ingest(
    request: Request,
    response: Response,
    active_controller: SimulationController = Depends(get_active_controller),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    webhook_signature: str | None = Header(default=None, alias="X-Webhook-Signature"),
    mock_friction: str | None = Header(
        default=None,
        alias="X-Mock-Friction",
        description="Comma-separated mock friction codes (invalid_key, subscription_inactive, rate_limit).",
    ),
) -> MockIngestResponse:
    """Mock RegEngine ingest, honouring the same headers the live webhook does.

    ``Idempotency-Key`` and friction codes are threaded through so a customer
    testing their own client against this route sees the same deduping and the
    same failure modes as the in-process simulator path. Signature
    verification is a no-op unless REGENGINE_WEBHOOK_HMAC_SECRET is set.

    The body is read and parsed by hand rather than declared as a handler
    parameter, because a declared parameter is bound by FastAPI *before* the
    handler runs — which put the whole batch through pydantic before the
    signature was ever checked. Live RegEngine verifies the signature first, so
    an unsigned caller there gets a 401 and learns nothing else; here it bought
    a full parse of an arbitrarily large batch and, on a malformed one, a 422
    enumerating every accepted CTE type. Signature verification is the
    outermost gate now: size ceiling, then HMAC, then everything else.
    """
    service = active_controller.mock_service
    try:
        raw_body = await _read_bounded_body(request)
        service.verify_signature(raw_body, webhook_signature)
        # Past this point the caller is authenticated (or no secret is set, in
        # which case there is nothing to authenticate against), so a detailed
        # 400/422 tells them nothing they were not entitled to know.
        friction = validate_friction_codes(_parse_mock_friction(mock_friction))
        payload = parse_ingest_payload(raw_body)
        body = service.ingest(
            payload,
            idempotency_key=idempotency_key,
            friction=friction,
        )
        _apply_event_age_headers(response, getattr(service, "last_age_window_warnings", ()))
        return body
    except MockRegEngineHTTPError as exc:
        # Without this the mock's own 400/401/402/413/422/429 escape as an
        # unhandled 500.
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _read_bounded_body(request: Request) -> bytes:
    """The raw request body, refused above ``MAX_INGEST_BODY_BYTES``.

    Checked against ``Content-Length`` first so an oversized body is refused
    without being read at all. The post-read check covers callers that send no
    length (chunked transfer encoding), where the bytes have to arrive before
    they can be counted.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_INGEST_BODY_BYTES:
        raise _body_too_large(int(declared))
    raw_body = await request.body()
    if len(raw_body) > MAX_INGEST_BODY_BYTES:
        raise _body_too_large(len(raw_body))
    return raw_body


def _body_too_large(size: int) -> MockRegEngineHTTPError:
    return MockRegEngineHTTPError(
        413,
        f"Request body is {size} bytes, above the {MAX_INGEST_BODY_BYTES} byte "
        f"ingest ceiling. Batches are capped at {MAX_BATCH_EVENTS} events; "
        "split the batch and retry.",
    )


def _apply_event_age_headers(response: Response, warnings: tuple[str, ...]) -> None:
    """Report the replay window on every ingest response.

    The mock defaults to accepting events older than RegEngine's 90-day
    window (the shipped demo fixtures predate it), so the fact that live
    would reject them has to be visible somewhere a client can assert on.
    These headers are always present, so a caller can check them
    unconditionally instead of inferring parity from an accepted count.
    """
    mode = event_age_mode()
    headers = {
        "X-Mock-Event-Age-Window-Days": str(max_event_age_days()),
        "X-Mock-Event-Age-Mode": mode,
        "X-Mock-Out-Of-Window-Events": str(len(warnings)),
        "Access-Control-Expose-Headers": (
            "X-Mock-Event-Age-Window-Days, X-Mock-Event-Age-Mode, "
            "X-Mock-Out-Of-Window-Events"
        ),
    }
    if warnings and mode == EVENT_AGE_MODE_WARN:
        # RegEngine's own wording carries an em dash; HTTP header values are
        # latin-1 only, so the header gets an ASCII-safe rendering while the
        # log line and the mock's own error strings keep the exact text.
        headers["X-Mock-Event-Age-Warning"] = _header_safe(
            f"{len(warnings)} event(s) accepted here would be rejected by live "
            f"RegEngine with 'replay window exceeded'. First: {warnings[0]}"
        )
        headers["Access-Control-Expose-Headers"] += ", X-Mock-Event-Age-Warning"
    response.headers.update(headers)


def _header_safe(value: str) -> str:
    return value.replace("\u2014", "-").encode("latin-1", "replace").decode("latin-1")


def _parse_mock_friction(raw: str | None) -> tuple[str, ...]:
    """Split the header. Validation of the codes is `validate_friction_codes`."""
    if not raw:
        return ()
    return tuple(code.strip() for code in raw.split(",") if code.strip())


@router.get("/export/presets", response_model=FDAExportPresetListResponse)
async def mock_fda_request_export_presets() -> FDAExportPresetListResponse:
    return FDAExportPresetListResponse(
        presets=[
            FDAExportPresetSummary.model_validate(summary)
            for summary in list_fda_export_preset_summaries()
        ]
    )


@router.get("/export/fda-request")
async def mock_fda_request_export(
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    preset: FDAExportPreset = Query(default=FDAExportPreset.ALL_RECORDS),
    traceability_lot_code: str | None = Query(default=None),
    limit: int = Query(
        default=EXPORT_DEFAULT_LIMIT,
        ge=1,
        le=EXPORT_MAX_LIMIT,
        description=(
            "Maximum records to write to the CSV, oldest event first. When more "
            "records match, the file is truncated, a '# PARTIAL EXPORT' banner is "
            "prepended above the header row, the filename is prefixed 'PARTIAL-', "
            "and X-Export-Truncated is true."
        ),
    ),
    active_controller: SimulationController = Depends(get_active_controller),
) -> PlainTextResponse:
    start_filter, end_filter = _parse_export_date_filters(start_date=start_date, end_date=end_date)
    definition = FDA_EXPORT_PRESETS[preset]
    if definition.requires_lot_code and not traceability_lot_code:
        raise HTTPException(status_code=400, detail="traceability_lot_code is required for this export preset")

    if traceability_lot_code:
        records = active_controller.store.lineage(traceability_lot_code)
        if not records:
            raise HTTPException(status_code=404, detail="No records found for that lot code")
        records = _filter_records_between(records, start_date=start_filter, end_date=end_filter)
    else:
        records = active_controller.store.all_between(
            start_date=start_filter.isoformat() if start_filter else None,
            end_date=end_filter.isoformat() if end_filter else None,
        )
    records = apply_fda_export_preset(records, preset)

    total_records = len(records)
    # Records are oldest-event-first, so the kept slice is the start of the
    # record set rather than an arbitrary window.
    records = records[:limit]
    truncated = total_records > len(records)

    csv_text = render_fda_request_csv(
        records,
        location_gln=_location_gln_resolver(active_controller, records),
    )
    if truncated:
        # A regulator-facing CSV must never look complete when it is not, so
        # the warning goes in the file itself, above the header row, where it
        # is the first thing any reader or spreadsheet import sees.
        csv_text = f"{_truncation_banner(total_records, len(records), limit)}\n{csv_text}"

    filename = export_filename(preset)
    if truncated:
        filename = f"PARTIAL-{filename}"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    headers.update(_export_bounds_headers(total_records, len(records), limit, truncated))
    return PlainTextResponse(content=csv_text, media_type="text/csv", headers=headers)


@router.get(
    "/export/epcis",
    response_model=EpcisDocumentResponse,
    response_model_exclude_none=True,
)
async def mock_epcis_export(
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    traceability_lot_code: str | None = Query(default=None),
    limit: int = Query(
        default=EXPORT_DEFAULT_LIMIT,
        ge=1,
        le=EXPORT_MAX_LIMIT,
        description=(
            "Maximum EPCIS events to include, oldest event first. When more "
            "records match, the document is truncated, carries a "
            "`regengine:exportSummary` member with `truncated` true, and the "
            "response sets X-Export-Truncated."
        ),
    ),
    active_controller: SimulationController = Depends(get_active_controller),
) -> JSONResponse:
    start_filter, end_filter = _parse_export_date_filters(start_date=start_date, end_date=end_date)
    if traceability_lot_code:
        records = active_controller.store.lineage(traceability_lot_code)
        if not records:
            raise HTTPException(status_code=404, detail="No records found for that lot code")
        records = _filter_records_between(records, start_date=start_filter, end_date=end_filter)
    else:
        records = active_controller.store.all_between(
            start_date=start_filter.isoformat() if start_filter else None,
            end_date=end_filter.isoformat() if end_filter else None,
        )

    total_records = len(records)
    records = records[:limit]
    truncated = total_records > len(records)

    document = render_epcis_document(
        records,
        source=active_controller.config.source,
        location_gln=_location_gln_resolver(active_controller, records),
    )
    # JSON-LD has no room for a comment banner, so the same numbers ride along
    # as a first-class member of the document. It is always present, so a
    # consumer that checks it once can trust it on every export.
    summary: dict[str, Any] = {
        "total_records": total_records,
        "returned_records": len(records),
        "limit": limit,
        "truncated": truncated,
    }
    if truncated:
        summary["warning"] = _truncation_banner(total_records, len(records), limit).lstrip("# ")
    document["regengine:exportSummary"] = summary

    filename = epcis_filename()
    if truncated:
        filename = f"PARTIAL-{filename}"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    headers.update(_export_bounds_headers(total_records, len(records), limit, truncated))
    return JSONResponse(content=document, media_type="application/ld+json", headers=headers)


def _truncation_banner(total_records: int, returned_records: int, limit: int) -> str:
    return (
        f"{TRUNCATION_BANNER_PREFIX}: {returned_records} of {total_records} matching "
        f"records included (limit={limit}). Records after the first "
        f"{returned_records} are MISSING from this file. Narrow the date range or "
        f"lot code, or re-request with a higher limit (max {EXPORT_MAX_LIMIT})."
    )


def _export_bounds_headers(
    total_records: int,
    returned_records: int,
    limit: int,
    truncated: bool,
) -> dict[str, str]:
    """Bound-reporting headers, mirroring the /api/lineage field names.

    Present on every export, truncated or not, so a client can assert on
    them unconditionally instead of inferring completeness from a row count.
    """
    headers = {
        "X-Export-Total-Records": str(total_records),
        "X-Export-Returned-Records": str(returned_records),
        "X-Export-Limit": str(limit),
        "X-Export-Truncated": "true" if truncated else "false",
        # Browsers only expose whitelisted headers to fetch(), and the
        # dashboard downloads these links directly, so opt them in.
        "Access-Control-Expose-Headers": (
            "X-Export-Total-Records, X-Export-Returned-Records, "
            "X-Export-Limit, X-Export-Truncated"
        ),
    }
    if truncated:
        headers["X-Export-Warning"] = _truncation_banner(total_records, returned_records, limit).lstrip("# ")
        headers["Access-Control-Expose-Headers"] += ", X-Export-Warning"
    return headers


def _location_gln_resolver(
    active_controller: SimulationController,
    records: list[StoredEventRecord],
) -> Callable[[str], str]:
    """GLN lookup for the exporters, with an event-supplied fallback.

    The exporters resolve a GLN from ``location_name`` against the engine's
    static location registry. Events that carry their own ``location_gln``
    (a CSV import, for instance) name locations the registry has never heard
    of, and their GLN would otherwise be dropped from both exports. This
    falls back to the GLN the events themselves supplied.
    """
    supplied: dict[str, str] = {}
    for record in records:
        event = record.event
        gln = getattr(event, "location_gln", None)
        if gln and event.location_name and event.location_name not in supplied:
            supplied[event.location_name] = gln

    registry_lookup = active_controller.engine.location_gln
    if not supplied:
        return registry_lookup

    def resolve(location_name: str) -> str:
        return registry_lookup(location_name) or supplied.get(location_name, "")

    return resolve


def _filter_records_between(
    records: list[Any],
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[Any]:
    filtered = []
    for record in records:
        day = record.event.timestamp.date()
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        filtered.append(record)
    return sorted(filtered, key=lambda record: record.event.timestamp)


def _parse_export_date_filters(
    start_date: str | None,
    end_date: str | None,
) -> tuple[date | None, date | None]:
    start_filter = _parse_export_date("start_date", start_date)
    end_filter = _parse_export_date("end_date", end_date)
    if start_filter and end_filter and start_filter > end_filter:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")
    return start_filter, end_filter


def _parse_export_date(field_name: str, value: str | None) -> date | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(status_code=400, detail=f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be a valid date") from exc
