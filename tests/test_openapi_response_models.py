"""Health and EPCIS export must publish a real response schema.

Regression cover for #146: `/api/health` and `/api/healthz` were typed
`-> dict[str, Any]` with no `response_model`, so the generated OpenAPI showed
`{"additionalProperties": true, "type": "object"}`, and the EPCIS export
returned a bare `JSONResponse` whose documented schema was literally `{}`.
"""

from __future__ import annotations


from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _response_schema(path: str, method: str = "get") -> dict:
    schema = app.openapi()
    content = schema["paths"][path][method]["responses"]["200"]["content"]
    body = content["application/json"]["schema"]
    ref = body.get("$ref")
    if ref:
        return schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    return body


def test_health_publishes_concrete_fields():
    model = _response_schema("/api/health")

    assert model.get("additionalProperties") is not True
    assert set(model["properties"]) == {
        "ok",
        "utc_time",
        "build",
        "contract_version",
        "tenant",
        "auth",
        "status",
    }


def test_healthz_publishes_concrete_fields():
    model = _response_schema("/api/healthz")

    assert model.get("additionalProperties") is not True
    assert set(model["properties"]) == {"ok", "utc_time", "build", "contract_version"}


def test_epcis_export_publishes_a_non_empty_schema():
    model = _response_schema("/api/mock/regengine/export/epcis")

    assert model != {}
    assert "@context" in model["properties"]
    assert "epcisBody" in model["properties"]


def test_health_endpoints_still_answer_the_documented_shape():
    for path in ("/api/health", "/api/healthz"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        body = response.json()
        documented = set(_response_schema(path)["properties"])
        assert documented <= set(body), f"{path} omitted documented fields"


def test_epcis_export_still_streams_the_jsonld_document():
    # The handler returns a JSONResponse directly so it can set the JSON-LD
    # media type and Content-Disposition; declaring response_model must not
    # have turned that into a plain serialised model.
    response = client.get("/api/mock/regengine/export/epcis")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/ld+json")
    assert "attachment;" in response.headers["content-disposition"]
    assert response.json()["type"] == "EPCISDocument"


def test_epcis_export_answers_the_shape_it_documents():
    """A documented schema that lies is worse than none at all.

    The health endpoints have `test_health_endpoints_still_answer_the_documented
    _shape` for exactly this; the EPCIS export had no equivalent, so the
    envelope could drift from `render_epcis_document` and OpenAPI would keep
    advertising the old keys.

    Also pins the two facts the `responses=`-rather-than-`response_model=`
    choice exists to preserve: the handler still returns its own JSONResponse,
    so the JSON-LD media type survives, and nothing re-serializes a payload
    whose shape GS1 owns.
    """
    client.post("/api/simulate/step")
    response = client.get("/api/mock/regengine/export/epcis")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/ld+json")

    documented = set(_response_schema("/api/mock/regengine/export/epcis")["properties"])
    assert set(response.json()) == documented, (
        "the EPCIS envelope and its published schema have drifted apart"
    )
