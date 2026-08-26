"""Bounds, response schema, and importer-fidelity coverage for the exports.

Truncating a compliance export is more dangerous than truncating a browsable
list: an operator can hand a regulator a CSV that looks complete. These tests
pin the loud signals - the in-file banner, the ``PARTIAL-`` filename, the
``X-Export-*`` headers, and the EPCIS ``regengine:exportSummary`` member - so
a silent truncation cannot creep back in.
"""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from app.csv_importer import parse_csv_import
from app.main import app, controller
from app.routers.mock_regengine import (
    EXPORT_DEFAULT_LIMIT,
    EXPORT_MAX_LIMIT,
    TRUNCATION_BANNER_PREFIX,
)
from app.schemas.domain import CSVImportType
from app.schemas.simulation import SimulationConfig


client = TestClient(app)

LINEAGE_CSV = """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,source_traceability_lot_code,input_traceability_lot_codes,reference_document_type,reference_document_number
harvesting,TLC-BOUND-HARVEST,Romaine Lettuce,120,cases,Valley Fresh Farms,2026-02-05T08:00:00Z,,,Harvest Log,HAR-001
initial_packing,TLC-BOUND-PACKED,Romaine Lettuce,112,cases,Coastal Packhouse,2026-02-05T10:00:00Z,TLC-BOUND-HARVEST,,Packout Record,PACK-001
shipping,TLC-BOUND-PACKED,Romaine Lettuce,112,cases,Coastal Packhouse,2026-02-05T12:00:00Z,,,Bill of Lading,BOL-001
receiving,TLC-BOUND-PACKED,Romaine Lettuce,112,cases,Distribution Center #4,2026-02-05T18:00:00Z,,,Bill of Lading,BOL-001
"""


def setup_function() -> None:
    import asyncio

    asyncio.run(controller.reset(SimulationConfig()))
    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "scheduled_events",
            "csv_text": LINEAGE_CSV,
            "delivery": {"mode": "none"},
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 4


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


# --- #145: bounded exports ------------------------------------------------


def test_fda_export_reports_bounds_when_not_truncated():
    response = client.get("/api/mock/regengine/export/fda-request")

    assert response.status_code == 200
    assert response.headers["X-Export-Total-Records"] == "4"
    assert response.headers["X-Export-Returned-Records"] == "4"
    assert response.headers["X-Export-Limit"] == str(EXPORT_DEFAULT_LIMIT)
    assert response.headers["X-Export-Truncated"] == "false"
    assert "X-Export-Warning" not in response.headers
    assert TRUNCATION_BANNER_PREFIX not in response.text
    assert "PARTIAL-" not in response.headers["Content-Disposition"]
    assert len(_csv_rows(response.text)) == 4


def test_fda_export_truncation_is_loud():
    response = client.get("/api/mock/regengine/export/fda-request?limit=2")

    assert response.status_code == 200
    assert response.headers["X-Export-Total-Records"] == "4"
    assert response.headers["X-Export-Returned-Records"] == "2"
    assert response.headers["X-Export-Truncated"] == "true"
    assert "2 of 4" in response.headers["X-Export-Warning"]
    # The filename an operator saves says so too.
    assert "PARTIAL-" in response.headers["Content-Disposition"]
    # And so does the very first line of the file itself.
    first_line, _, remainder = response.text.partition("\n")
    assert first_line.startswith(TRUNCATION_BANNER_PREFIX)
    assert "MISSING" in first_line
    assert str(EXPORT_MAX_LIMIT) in first_line
    rows = _csv_rows(remainder)
    assert len(rows) == 2


def test_fda_export_rejects_out_of_range_limit():
    assert client.get("/api/mock/regengine/export/fda-request?limit=0").status_code == 422
    assert (
        client.get(f"/api/mock/regengine/export/fda-request?limit={EXPORT_MAX_LIMIT + 1}").status_code
        == 422
    )


def test_epcis_export_reports_bounds_when_not_truncated():
    response = client.get("/api/mock/regengine/export/epcis")

    assert response.status_code == 200
    assert response.headers["X-Export-Truncated"] == "false"
    document = response.json()
    summary = document["regengine:exportSummary"]
    assert summary == {
        "total_records": 4,
        "returned_records": 4,
        "limit": EXPORT_DEFAULT_LIMIT,
        "truncated": False,
    }
    assert len(document["epcisBody"]["eventList"]) == 4


def test_epcis_export_truncation_is_loud():
    response = client.get("/api/mock/regengine/export/epcis?limit=1")

    assert response.status_code == 200
    assert response.headers["X-Export-Total-Records"] == "4"
    assert response.headers["X-Export-Returned-Records"] == "1"
    assert response.headers["X-Export-Truncated"] == "true"
    assert "PARTIAL-" in response.headers["Content-Disposition"]
    document = response.json()
    summary = document["regengine:exportSummary"]
    assert summary["truncated"] is True
    assert summary["total_records"] == 4
    assert summary["returned_records"] == 1
    assert "MISSING" in summary["warning"]
    assert len(document["epcisBody"]["eventList"]) == 1


def test_lot_scoped_export_is_bounded_too():
    response = client.get(
        "/api/mock/regengine/export/fda-request"
        "?preset=lot_trace&traceability_lot_code=TLC-BOUND-PACKED&limit=1"
    )

    assert response.status_code == 200
    assert response.headers["X-Export-Truncated"] == "true"
    assert response.text.startswith(TRUNCATION_BANNER_PREFIX)


# --- #146: EPCIS response schema -----------------------------------------


def test_epcis_export_has_a_documented_openapi_schema():
    schema = app.openapi()
    responses = schema["paths"]["/api/mock/regengine/export/epcis"]["get"]["responses"]
    content = responses["200"]["content"]
    (media_type,) = content.keys()
    ref = content[media_type]["schema"]["$ref"]
    assert ref.endswith("/EpcisDocumentResponse")

    model = schema["components"]["schemas"]["EpcisDocumentResponse"]
    assert {"@context", "type", "schemaVersion", "creationDate", "epcisBody"} <= set(
        model["properties"]
    )
    assert "regengine:exportSummary" in model["properties"]


def test_rendered_epcis_document_validates_against_the_response_model():
    from app.schemas.exports import EpcisDocumentResponse

    document = client.get("/api/mock/regengine/export/epcis").json()
    parsed = EpcisDocumentResponse.model_validate(document)
    assert parsed.schemaVersion == "2.0"
    assert parsed.export_summary is not None
    assert parsed.export_summary.truncated is False


# --- #160: ragged CSV rows -----------------------------------------------


def test_row_with_extra_columns_is_rejected():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp
harvesting,TLC-RAGGED,Romaine, Hearts,120,cases,Valley Fresh Farms,2026-02-05T08:00:00Z
""",
    )

    assert parsed.total == 1
    assert parsed.events == []
    assert [(error.row, error.field) for error in parsed.errors] == [(2, "row")]
    message = parsed.errors[0].message
    assert "beyond the 7 header column(s)" in message
    assert "2026-02-05T08:00:00Z" in message


def test_trailing_empty_field_is_still_importable():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp
harvesting,TLC-TRAILING,Romaine Lettuce,120,cases,Valley Fresh Farms,2026-02-05T08:00:00Z,
""",
    )

    assert parsed.errors == []
    assert len(parsed.events) == 1


# --- #162: ambiguous column mapping --------------------------------------


def test_colliding_kde_columns_are_rejected():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,vessel_identifier,kde_vessel_identifier
harvesting,TLC-COLLIDE,Gulf Oysters,20,bushels,Valley Fresh Farms,2026-02-05T08:00:00Z,VESSEL-REAL-001,VESSEL-BOGUS-999
""",
    )

    assert parsed.events == []
    (error,) = [error for error in parsed.errors if error.field == "kde_vessel_identifier"]
    assert "vessel_identifier" in error.message
    assert error.row == 2


def test_location_gln_column_populates_the_event_field():
    parsed = parse_csv_import(
        CSVImportType.SCHEDULED_EVENTS,
        """cte_type,traceability_lot_code,product_description,quantity,unit_of_measure,location_name,timestamp,location_gln
harvesting,TLC-GLN,Romaine Lettuce,120,cases,Unlisted Contract Farm,2026-02-05T08:00:00Z,0812345000013
""",
    )

    assert parsed.errors == []
    event = parsed.events[0]
    assert event.location_gln == "0812345000013"
    assert "location_gln" not in event.kdes


def test_imported_location_gln_reaches_both_exports():
    import asyncio

    asyncio.run(controller.reset(SimulationConfig()))
    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "scheduled_events",
            "csv_text": (
                "cte_type,traceability_lot_code,product_description,quantity,"
                "unit_of_measure,location_name,timestamp,location_gln,"
                "reference_document_type,reference_document_number\n"
                "harvesting,TLC-GLN-EXPORT,Romaine Lettuce,120,cases,"
                "Unlisted Contract Farm,2026-02-05T08:00:00Z,0812345000013,"
                "Harvest Log,HAR-900\n"
            ),
            "delivery": {"mode": "none"},
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1

    csv_response = client.get("/api/mock/regengine/export/fda-request")
    (row,) = _csv_rows(csv_response.text)
    assert row["Location Identifier (GLN)"] == "0812345000013"

    epcis_response = client.get("/api/mock/regengine/export/epcis")
    assert "0812345000013" in epcis_response.text
