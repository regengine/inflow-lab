from __future__ import annotations

from fastapi import APIRouter, Depends

from ..controller import SimulationController, request_with_stored_delivery_secrets
from ..dependencies import get_active_controller
from ..schemas.ingestion import CSVImportRequest, CSVImportResponse, DeliveryRetryRequest, DeliveryRetryResponse


router = APIRouter(prefix="/api", tags=["Ingestion"])


@router.post("/import/csv", response_model=CSVImportResponse)
async def import_csv(
    import_request: CSVImportRequest,
    active_controller: SimulationController = Depends(get_active_controller),
) -> CSVImportResponse:
    # #148: an omitted api_key/tenant_id in an inline delivery block means
    # "unchanged" -- the console's form is never given the stored secret back.
    return await active_controller.import_csv(
        request_with_stored_delivery_secrets(active_controller, import_request)
    )


@router.post("/delivery/retry", response_model=DeliveryRetryResponse)
async def retry_failed_delivery(
    retry_request: DeliveryRetryRequest | None = None,
    active_controller: SimulationController = Depends(get_active_controller),
) -> DeliveryRetryResponse:
    return await active_controller.retry_failed_delivery(
        request_with_stored_delivery_secrets(active_controller, retry_request)
    )
