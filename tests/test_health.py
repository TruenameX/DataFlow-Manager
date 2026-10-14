import pytest
from fastapi.testclient import TestClient

from dataflow_manager.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_returns_200(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_health_returns_expected_body(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_health_returns_json_content_type(client: TestClient) -> None:
    assert client.get("/health").headers["content-type"] == "application/json"
