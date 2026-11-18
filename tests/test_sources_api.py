"""Offline HTTP contract tests; the service is replaced at its boundary."""

from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from dataflow_manager.api import routes
from dataflow_manager.main import app
from dataflow_manager.services.sources import ServiceError, SourceSnapshot


class FakeEngine:
    def dispose(self):
        pass


@pytest.fixture
def client(monkeypatch):
    import dataflow_manager.main as main
    monkeypatch.setattr(main, "_initialize_database", lambda: (FakeEngine(), object()))
    with TestClient(app) as test_client:
        yield test_client


def _snapshot(identifier=None):
    now = datetime(2026, 1, 2, 3, 4, 5, 12, tzinfo=timezone.utc)
    return SourceSnapshot(identifier or uuid4(), "Source", "example.test", 5432,
                          "db", "user", now, now)


def _valid():
    return {"name": "Source", "host": "example.test", "port": 5432,
            "database": "db", "username": "user", "password": "synthetic-secret"}


def test_create_projects_safe_fields_and_location(client, monkeypatch):
    item = _snapshot()
    class FakeService:
        def __init__(self, factory, crypto): pass
        def create(self, values):
            assert values == _valid()
            return item
    monkeypatch.setattr(routes, "SourceService", FakeService)
    response = client.post("/api/v1/sources", json=_valid())
    assert response.status_code == 201
    assert response.headers["location"] == f"/api/v1/sources/{item.id}"
    assert response.json() == {"id": str(item.id), "name": "Source", "host": "example.test",
                               "port": 5432, "database": "db", "username": "user",
                               "created_at": "2026-01-02T03:04:05.000012Z",
                               "updated_at": "2026-01-02T03:04:05.000012Z"}
    assert "synthetic-secret" not in response.text


def test_crud_routes_and_missing_record(client, monkeypatch):
    item = _snapshot()
    class FakeService:
        def __init__(self, factory, crypto): pass
        def list(self): return [item]
        def get(self, identifier):
            if identifier != item.id: raise ServiceError("source_not_found")
            return item
        def update(self, identifier, values):
            assert identifier == item.id and values == {"name": "Other"}
            return item
        def delete(self, identifier): assert identifier == item.id
    monkeypatch.setattr(routes, "SourceService", FakeService)
    assert client.get("/api/v1/sources").json()[0]["id"] == str(item.id)
    assert client.get(f"/api/v1/sources/{item.id}").status_code == 200
    assert client.get(f"/api/v1/sources/{uuid4()}").json()["error"]["code"] == "source_not_found"
    assert client.patch(f"/api/v1/sources/{item.id}", json={"name": "Other"}).status_code == 200
    response = client.delete(f"/api/v1/sources/{item.id}")
    assert response.status_code == 204 and response.content == b""


def test_validation_precedence_and_secret_projection(client):
    url = "/api/v1/sources"
    assert client.post(url, content=b"").status_code == 422
    assert client.post(url, content=b"{", headers={"content-type": "text/plain"}).status_code == 415
    assert client.post(url, content=b"{", headers={"content-type": "application/json"}).status_code == 400
    bad = dict(_valid(), port=True, password="", leaked_extra="synthetic-secret")
    response = client.post(url + "?secret=synthetic-secret", json=bad)
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == ["port", "password", "request"]
    assert "synthetic-secret" not in response.text
    assert client.post(url, json=dict(_valid(), password="a\x00b")).json()["error"]["fields"] == ["password"]
    assert client.request("GET", url, content=b"{}").status_code == 422
    assert client.patch(url + "/bad-id?extra=1", json={"password": None}).json()["error"]["fields"] == ["id", "password", "request"]


def test_unavailable_and_routing_errors_are_safe(client, monkeypatch):
    app.state._dfm_db_resources = None
    assert client.get("/api/v1/sources").json()["error"]["code"] == "operation_unavailable"
    assert client.get("/missing").json()["error"]["code"] == "route_not_found"
    response = client.put("/api/v1/sources")
    assert response.status_code == 405 and "allow" in response.headers
    assert client.get("/health").json() == {"status": "ok"}


def test_openapi_declares_crud_and_request_schemas(client):
    schema = client.get("/openapi.json").json()
    endpoints = schema["paths"]["/api/v1/sources"]
    assert {"get", "post"} <= endpoints.keys()
    assert "requestBody" in endpoints["post"]
    assert "Location" in endpoints["post"]["responses"]["201"]["headers"]
    assert "error" in endpoints["post"]["responses"]["422"]["content"]["application/json"]["schema"]["properties"]
    validation = endpoints["post"]["responses"]["422"]["content"]["application/json"]["schema"]["properties"]["error"]
    internal = endpoints["post"]["responses"]["500"]["content"]["application/json"]["schema"]["properties"]["error"]
    assert "fields" in validation["required"] and "fields" not in internal["properties"]
    assert {"get", "patch", "delete"} <= schema["paths"]["/api/v1/sources/{source_id}"].keys()


def test_response_validation_and_unexpected_exception_do_not_expose_secrets(client, monkeypatch, caplog, capsys):
    marker = "synthetic-credential-marker-5d82"
    caplog.set_level(logging.WARNING)
    logging.getLogger("positive-control").warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()
    class FakeService:
        mode = "validation"
        def __init__(self, factory, crypto): pass
        def get(self, identifier):
            if self.mode == "error": raise RuntimeError(marker)
            return SimpleNamespace(id=marker, name=marker, host="h", port=1, database="d", username="u",
                                   created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    monkeypatch.setattr(routes, "SourceService", FakeService)
    path = f"/api/v1/sources/{uuid4()}"
    for mode in ("validation", "error"):
        FakeService.mode = mode
        response = client.get(path)
        assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
        assert marker not in response.text
    output = capsys.readouterr()
    assert marker not in output.out + output.err + caplog.text
    for record in caplog.records:
        assert marker not in str(record.msg) + repr(record.args)
        assert record.exc_info is None and record.stack_info is None


def test_json_media_type_accepts_standard_parameter_whitespace(client):
    response = client.post("/api/v1/sources", content=b"{}", headers={"content-type": "application/json; charset = UTF-8"})
    assert response.status_code == 422
    encoded = client.post("/api/v1/sources", content=b"{}", headers={"content-type": "application/json; charset*=utf-8''utf-8"})
    assert encoded.status_code == 415 and encoded.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numeric_constants_are_malformed(client, literal):
    response = client.post("/api/v1/sources", content='{"port":' + literal + '}',
                           headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "malformed_json"
