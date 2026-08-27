from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..epcis_export import epcis_filename, render_epcis_document
from ..fda_export import (
    FDA_EXPORT_PRESETS,
    apply_fda_export_preset,
    export_filename,
    list_fda_export_preset_summaries,
    render_fda_request_csv,
)
from ..mock_service import MockRegEngineHTTPError, verify_webhook_signature
from ..schemas.domain import FDAExportPreset
from ..schemas.exports import FDAExportPresetListResponse, FDAExportPresetSummary
from ..schemas.ingestion import IngestPayload, MockIngestResponse


router = APIRouter(prefix="/api/mock/regengine", tags=["Mock RegEngine"])


def _ingest_request_body_schema() -> dict[str, Any]:
    """OpenAPI requestBody for /ingest, derived from IngestPayload itself.

    The route reads and validates its body by hand (see the comment in
    mock_regengine_ingest for why), and a body FastAPI does not see is a
    body FastAPI cannot document -- which would have quietly dropped the
    ingest contract out of /docs, in the one repo whose reason for existing
    is letting an integrator read that contract before they wire against it.

    Generated from the model rather than transcribed, so it cannot drift
    from what the route actually accepts. $defs is dropped because the
    ref_template points nested models at the document's own components,
    where FastAPI already emits them via the routes that return
    StoredEventRecord; tests/test_api_robustness.py pins that those
    references resolve.
    """
    schema = IngestPayload.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


@router.post(
    "/ingest",
    response_model=MockIngestResponse,
    openapi_extra=_ingest_request_body_schema(),
)
async def mock_regengine_ingest(
    request: Request,
    active_controller: SimulationController = Depends(get_active_controller),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_webhook_signature: str | None = Header(default=None, alias="X-Webhook-Signature"),
    x_mock_friction: str | None = Header(default=None, alias="X-Mock-Friction"),
) -> MockIngestResponse:
    # The body is read and parsed BY HAND here, rather than declared as a
    # `payload: IngestPayload` parameter, so that HMAC verification is
    # genuinely the outermost gate (#209).
    #
    # A declared body parameter is not a gate you can get in front of:
    # FastAPI reads the body, json.loads() it, and only then solves
    # dependencies -- so a 50 MB unsigned body was fully decoded and
    # model-validated before the 401 (measured at ~460 MB RSS and 2.4s,
    # ~12x amplification, from an unauthenticated caller). Worse, an
    # unsigned caller got a schema oracle out of it: per-field 422s
    # enumerating the payload shape where they should only ever have seen a
    # flat 401.
    #
    # Reading the bytes is unavoidable -- HMAC is computed over them -- but
    # everything downstream of the bytes now happens only after the
    # signature checks out. Verifying against the real wire bytes rather
    # than a round-tripped re-serialization of a parsed model is itself the
    # point of the check; see verify_webhook_signature's docstring (#113).
    raw_body = await request.body()

    try:
        verify_webhook_signature(raw_body, x_webhook_signature)
    except MockRegEngineHTTPError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    payload = _parse_ingest_payload(raw_body)

    # X-Mock-Friction has no live-RegEngine equivalent. It is how a caller
    # hitting this route directly -- "as a customer testing their own
    # client integration would" (#116), rather than going through the
    # simulator's own DeliveryConfig.mock_friction -- can still rehearse
    # the same auth/billing/rate-limit failure modes the internal delivery
    # path already exercises. Comma-separated so more than one code can be
    # combined, same as DeliveryConfig.mock_friction being a list.
    friction = (
        tuple(code.strip() for code in x_mock_friction.split(",") if code.strip())
        if x_mock_friction
        else ()
    )

    # mock_service.ingest() raises MockRegEngineHTTPError for request-level
    # rejections (e.g. a >500-event batch, injected friction, or a bad/
    # missing signature) that mirror a live non-2xx RegEngine response.
    # Nothing else in the app registers a handler for that exception type,
    # so left uncaught it becomes an unhandled 500 instead of the
    # descriptive 4xx a real caller gets (#142). Other callers of ingest()
    # (step/replay/csv-import/fixtures, via
    # SimulationController._deliver_payload) already catch it themselves.
    try:
        return active_controller.mock_service.ingest(
            payload,
            idempotency_key=idempotency_key,
            friction=friction,
            raw_body=raw_body,
            signature_header=x_webhook_signature,
        )
    except MockRegEngineHTTPError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _parse_ingest_payload(raw_body: bytes) -> IngestPayload:
    """Decode and validate the ingest body the way FastAPI would have.

    Hand-rolled only because the parse has to happen *after* signature
    verification (#209); the observable result is deliberately identical to
    a declared `payload: IngestPayload` parameter, so #143's
    extra="forbid" rejections keep returning the same 422 body they always
    did. Both shapes below are copied from FastAPI's own request handler:
    a JSON decode failure becomes a `json_invalid` error located at the
    byte offset, and Pydantic's errors are re-rooted under "body" exactly
    as ModelField.validate does before handing them to
    RequestValidationError.
    """
    if not raw_body:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("body",), "msg": "Field required", "input": None}]
        )
    try:
        decoded = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body", exc.pos),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": exc.msg},
                }
            ]
        ) from exc
    try:
        return IngestPayload.model_validate(decoded)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("body", *tuple(error.get("loc", ())))}
                for error in exc.errors(include_url=False)
            ]
        ) from exc


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
    csv_text = render_fda_request_csv(records, location_gln=active_controller.engine.location_gln)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={export_filename(preset)}"},
    )


@router.get("/export/epcis")
async def mock_epcis_export(
    start_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Inclusive YYYY-MM-DD"),
    traceability_lot_code: str | None = Query(default=None),
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

    document = render_epcis_document(
        records,
        source=active_controller.config.source,
        location_gln=active_controller.engine.location_gln,
    )
    return JSONResponse(
        content=document,
        media_type="application/ld+json",
        headers={"Content-Disposition": f"attachment; filename={epcis_filename()}"},
    )


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
