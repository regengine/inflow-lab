from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class TenantSummary(BaseModel):
    tenant_id: str
    cached: bool
    running: bool
    total_records: int
    scenario_save_count: int
    persist_path: str
    data_path: str
    exists_on_disk: bool


class TenantListResponse(BaseModel):
    tenants: list[TenantSummary]


class TenantOperationResponse(BaseModel):
    status: Literal["reset", "deleted"]
    tenant_id: str
    removed_cached_controller: bool = False
    removed_data: bool = False
