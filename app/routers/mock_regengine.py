from __future__ import annotations

import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from ..controller import SimulationController
from ..dependencies import get_active_controller
from ..epcis_export import epcis_filename, render_epcis_document
from ..mock_service import MockRegEngineHTTPError
from ..fda_export import (
    FDA_EXPORT_PRESETS,
    apply_fda_export_preset,
    export_filename,
    list_fda_export_preset_summaries,
    render_fda_request_csv,
)
from ..schemas.domain import FDAExportPreset
from ..schemas.exports import FDAExportPresetListResponse, FDAExportPresetSummary
from ..schemas.ingestion import IngestPayload, MockIngestResponse


router = APIRouter(prefix="/api/mock/regengine", tags=["Mock RegEngine"])


@router.post("/ingest", response_model=MockIngestResponse)
async def mock_regengine_ingest(
    request: Request,
    payload: IngestPayload,
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
    """
    service = active_controller.mock_service
    try:
        # Starlette caches the body FastAPI already read, so this is the exact
        # byte sequence the client signed — the point of verifying at all.
        service.verify_signature(await request.body(), webhook_signature)
        return service.ingest(
            payload,
            idempotency_key=idempotency_key,
            friction=_parse_mock_friction(mock_friction),
        )
    except MockRegEngineHTTPError as exc:
        # Without this the mock's own 401/402/422/429 escape as an unhandled 500.
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _parse_mock_friction(raw: str | None) -> tuple[str, ...]:
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
