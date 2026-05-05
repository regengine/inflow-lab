from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class CTEType(str, Enum):
    HARVESTING = "harvesting"
    COOLING = "cooling"
    INITIAL_PACKING = "initial_packing"
    # First land-based receiving — RegEngine's WebhookCTEType supports
    # this CTE per 21 CFR §1.1325 (seafood / first-receiver flows). The
    # default LegitFlowEngine doesn't emit it yet because the current
    # scenarios are leafy-greens / fresh-cut / retailer-handoff. Including
    # the value keeps it valid for CSV imports, hand-crafted fixtures, and
    # future seafood scenarios so the simulator can exercise the same
    # webhook code path RegEngine validates against.
    FIRST_LAND_BASED_RECEIVING = "first_land_based_receiving"
    SHIPPING = "shipping"
    RECEIVING = "receiving"
    TRANSFORMATION = "transformation"


class DestinationMode(str, Enum):
    MOCK = "mock"
    LIVE = "live"
    NONE = "none"


class CSVImportType(str, Enum):
    SCHEDULED_EVENTS = "scheduled_events"
    SEED_LOTS = "seed_lots"


class FDAExportPreset(str, Enum):
    ALL_RECORDS = "all_records"
    LOT_TRACE = "lot_trace"
    SHIPMENT_HANDOFF = "shipment_handoff"
    RECEIVING_LOG = "receiving_log"
    TRANSFORMATION_BATCHES = "transformation_batches"


class DemoFixtureId(str, Enum):
    LEAFY_GREENS_TRACE = "leafy_greens_trace"
    FRESH_CUT_TRANSFORMATION = "fresh_cut_transformation"
    RETAILER_HANDOFF = "retailer_handoff"


class RegEngineEvent(BaseModel):
    cte_type: CTEType
    traceability_lot_code: str
    product_description: str
    quantity: float
    unit_of_measure: str
    location_name: str
    timestamp: datetime
    kdes: dict[str, Any] = Field(default_factory=dict)


class StoredEventRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    sequence_no: int = 0
    payload_source: str
    event: RegEngineEvent
    parent_lot_codes: list[str] = Field(default_factory=list)
    destination_mode: DestinationMode = DestinationMode.NONE
    delivery_status: Literal["generated", "posted", "failed"] = "generated"
    delivery_attempts: int = 0
    last_delivery_attempt_at: datetime | None = None
    last_delivery_success_at: datetime | None = None
    delivery_response: dict[str, Any] | None = None
    delivery_metadata: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LineageNode(BaseModel):
    lot_code: str
    product_description: str
    event_count: int
    cte_types: list[CTEType]
    first_seen: datetime
    last_seen: datetime
    locations: list[str]


class LineageEdge(BaseModel):
    source_lot_code: str
    target_lot_code: str
    cte_type: CTEType
    event_sequence_no: int
