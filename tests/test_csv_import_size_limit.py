"""#136's second acceptance criterion: `csv_text` has an enforced maximum size.

The first criterion ("a large store write or CSV import no longer delays an
unrelated concurrent request") is already met and covered by
``tests/test_store_async.py``: ``EventStore``'s write paths and the
CPU-bound ``parse_csv_import`` both run on a worker thread via
``asyncio.to_thread``, so the single event loop stays free. What that does
NOT do is bound the work. One request can still hand the parser an
arbitrarily large body -- pinning a pool thread and holding several copies
of the text in memory (raw body, decoded str, parsed rows, built events)
for as long as it takes -- which is why the issue asks for both.

``CSVImportRequest.csv_text`` carried no length constraint at all
(``app/schemas/ingestion.py``, the exact line the issue cites). It now
refuses anything above ``MAX_CSV_TEXT_CHARS``.

Kept in its own file: parallel workstreams own tests/test_csv_importer.py,
tests/test_importer_errors.py and tests/test_importer_robustness.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.domain import CSVImportType
from app.schemas.ingestion import MAX_CSV_TEXT_CHARS, CSVImportRequest


client = TestClient(app)

SEED_HEADER = (
    "traceability_lot_code,product_description,quantity,"
    "unit_of_measure,location_name,reference_document"
)


def _csv_of_length(total_chars: int) -> str:
    """A syntactically valid seed_lots CSV padded to exactly *total_chars*.

    Padding goes into the product_description of one row rather than being
    appended as junk, so the body stays parseable -- the point is to test
    the size gate, not to have the parser reject it for some other reason
    if the gate ever stops firing.
    """
    prefix = f"{SEED_HEADER}\nTLC-SIZE-1,"
    suffix = ",10,cases,Valley Fresh Farms,HL-SIZE-1\n"
    padding = total_chars - len(prefix) - len(suffix)
    assert padding > 0, "requested length is too small to build a valid row"
    return prefix + ("R" * padding) + suffix


def test_a_body_at_the_limit_is_still_accepted():
    """The boundary from below. A cap that is off by one, or that measures
    something other than what it claims, shows up here.
    """
    csv_text = _csv_of_length(MAX_CSV_TEXT_CHARS)
    assert len(csv_text) == MAX_CSV_TEXT_CHARS

    request = CSVImportRequest(import_type=CSVImportType.SEED_LOTS, csv_text=csv_text)

    assert len(request.csv_text) == MAX_CSV_TEXT_CHARS


def test_a_body_one_character_over_the_limit_is_refused():
    """The boundary from above.

    Verified to fail before the fix: ``csv_text: str`` carried no
    constraint, so this constructed happily -- as did a body of any size at
    all.
    """
    csv_text = _csv_of_length(MAX_CSV_TEXT_CHARS + 1)

    with pytest.raises(ValidationError) as excinfo:
        CSVImportRequest(import_type=CSVImportType.SEED_LOTS, csv_text=csv_text)

    message = str(excinfo.value)
    assert "csv_text" in message
    # The operator is told the size they sent, the ceiling, and the remedy --
    # not just that a constraint failed.
    assert str(MAX_CSV_TEXT_CHARS + 1) in message
    assert str(MAX_CSV_TEXT_CHARS) in message
    assert "Split the file" in message


def test_the_route_refuses_an_oversized_import_with_a_422(monkeypatch):
    """End to end through POST /api/import/csv.

    Also pins that the refusal happens *before* the parse: an oversized
    body must cost a length check, not a full CPU-bound parse of the very
    text the limit exists to avoid parsing.
    """
    import app.controller as controller_module

    def _must_not_parse(*args, **kwargs):  # pragma: no cover - asserts by raising
        raise AssertionError("parse_csv_import ran on a body that exceeds the size limit")

    monkeypatch.setattr(controller_module, "parse_csv_import", _must_not_parse)

    response = client.post(
        "/api/import/csv",
        json={
            "import_type": "seed_lots",
            "csv_text": _csv_of_length(MAX_CSV_TEXT_CHARS + 1),
            "delivery": {"mode": "none"},
        },
    )

    assert response.status_code == 422
    detail = str(response.json()["detail"])
    assert "csv_text" in detail
    assert str(MAX_CSV_TEXT_CHARS) in detail


def test_an_ordinary_import_body_is_nowhere_near_the_limit():
    """A guard against a future limit set so low it breaks real use.

    The README's own documented example and the shipped CSV shapes are
    ~130 characters per row; the ceiling has to leave thousands of rows of
    headroom or it stops being a safety bound and starts being a bug.
    """
    ordinary = "\n".join(
        [SEED_HEADER]
        + [
            f"TLC-ORDINARY-{index},Romaine Hearts,10,cases,Valley Fresh Farms,HL-{index}"
            for index in range(1000)
        ]
    )

    request = CSVImportRequest(import_type=CSVImportType.SEED_LOTS, csv_text=ordinary)

    assert len(request.csv_text) < MAX_CSV_TEXT_CHARS
    # A thousand rows must not consume more than a small fraction of the
    # allowance, or "split the file" becomes routine advice.
    assert len(request.csv_text) * 8 < MAX_CSV_TEXT_CHARS
