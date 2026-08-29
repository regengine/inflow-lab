"""The shared FastAPI TestClient and per-test reset used by the API test
modules split out of tests/test_api.py for #132.

``app.main``'s ``app``/``controller`` are process-wide singletons, so the
split modules keep sharing one TestClient and one reset helper rather than
each building their own -- that is exactly what the single pre-split
tests/test_api.py did, so the split changes no behaviour.

The reset stays an explicit helper each module calls from its own
``setup_function`` rather than an autouse fixture in tests/conftest.py: a
conftest fixture would apply to every file under tests/, handing ~45 other
modules a controller reset before each test that they do not get today.
"""

import asyncio
import base64

from fastapi.testclient import TestClient

from app.main import app, controller
from app.schemas.simulation import SimulationConfig

client = TestClient(app)


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def reset_app_state() -> None:
    """Reset the shared app state between tests.

    Verbatim body of the pre-split tests/test_api.py ``setup_function``.
    """
    asyncio.run(controller.reset(SimulationConfig()))
