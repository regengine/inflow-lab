from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from .engine import LegitFlowEngine
from .schemas.ingestion import IngestPayload, IngestResponseEvent, MockIngestResponse


class MockRegEngineService:
    def __init__(self) -> None:
        self._chain_hash = ""

    def reset(self) -> None:
        self._chain_hash = ""

    def ingest(self, payload: IngestPayload) -> MockIngestResponse:
        response_events: list[IngestResponseEvent] = []
        accepted = 0
        rejected = 0
        for event in payload.events:
            raw = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            sha256_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            chain_seed = f"{self._chain_hash}:{sha256_hash}".encode("utf-8")
            self._chain_hash = hashlib.sha256(chain_seed).hexdigest()
            response_events.append(
                IngestResponseEvent(
                    traceability_lot_code=event.traceability_lot_code,
                    cte_type=event.cte_type,
                    status="accepted",
                    event_id=str(uuid4()),
                    sha256_hash=sha256_hash,
                    chain_hash=self._chain_hash,
                )
            )
            accepted += 1
        return MockIngestResponse(
            accepted=accepted,
            rejected=rejected,
            total=accepted + rejected,
            events=response_events,
            ingestion_timestamp=datetime.now(UTC),
        )
