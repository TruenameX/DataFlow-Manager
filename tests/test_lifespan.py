"""Offline FastAPI lifecycle checks with synthetic infrastructure."""

import importlib.util
import logging
import os
import socket
import sys

import psycopg
import pytest
import sqlalchemy
from fastapi.testclient import TestClient

import dataflow_manager.main as main
import dataflow_manager.db.session as session_module


@pytest.fixture
def environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("PG") or key.startswith("DFM_DB_"):
            monkeypatch.delenv(key, raising=False)
    values = {"HOST": "127.0.0.1", "PORT": "5432", "NAME": "test_db", "USER": "test_user",
              "PASSWORD": "synthetic-password", "POOL_SIZE": "2", "MAX_OVERFLOW": "1",
              "POOL_TIMEOUT": "5", "CONNECT_TIMEOUT": "3", "POOL_PRE_PING": "true",
              "POOL_RECYCLE": "1800", "ISOLATION_LEVEL": "READ COMMITTED"}
    for key, value in values.items():
        monkeypatch.setenv("DFM_DB_" + key, value)
    return values


class FakeEngine:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def dispose(self):
        self.calls += 1
        if self.error:
            raise self.error


def assert_health(client):
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"


def test_success_and_two_sequential_lifecycles(environment, monkeypatch):
    engines = []
    factories = []
    def factory(config):
        assert config.password == environment["PASSWORD"]
        engine, maker = FakeEngine(), object()
        engines.append(engine)
        factories.append(maker)
        return engine, maker
    monkeypatch.setattr(main, "_create_intermediate_resources", factory)
    for iteration in range(2):
        with TestClient(main.app) as client:
            assert main.app.state._dfm_db_resources == (engines[iteration], factories[iteration])
            assert engines[iteration].calls == 0
            assert_health(client)
        assert main.app.state._dfm_db_resources is None
        assert engines[iteration].calls == 1
    assert engines[0] is not engines[1]


@pytest.mark.parametrize("invalid", ["missing", "invalid"])
def test_invalid_config_fails_closed_without_factory(environment, monkeypatch, caplog, invalid):
    if invalid == "missing": monkeypatch.delenv("DFM_DB_PASSWORD")
    else: monkeypatch.setenv("DFM_DB_PORT", "not-a-number")
    def forbidden(config): raise AssertionError("factory must not run")
    monkeypatch.setattr(main, "_create_intermediate_resources", forbidden)
    with caplog.at_level(logging.ERROR):
        with TestClient(main.app) as client:
            assert main.app.state._dfm_db_resources is None
            assert_health(client)
    assert ("DB_CONFIG_MISSING" if invalid == "missing" else "DB_CONFIG_INVALID") in caplog.text
    assert environment["PASSWORD"] not in caplog.text


def test_factory_failure_does_not_publish_or_dispose_unowned(environment, monkeypatch, caplog):
    secret = "synthetic-factory-password-dsn-SQL"
    partial = FakeEngine()
    def factory(config):
        partial.dispose()  # Factory retains ownership when it fails before returning.
        raise RuntimeError(secret)
    monkeypatch.setattr(main, "_create_intermediate_resources", factory)
    with caplog.at_level(logging.ERROR):
        with TestClient(main.app) as client:
            assert main.app.state._dfm_db_resources is None
            assert_health(client)
    assert partial.calls == 1 and "DB_ENGINE_INIT_FAILED" in caplog.text
    assert secret not in caplog.text
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)


def test_dispose_failure_is_safe_and_health_stays_available(environment, monkeypatch, caplog):
    secret = "synthetic-dispose-password-dsn-SQL"
    engine = FakeEngine(RuntimeError(secret))
    monkeypatch.setattr(main, "_create_intermediate_resources", lambda config: (engine, object()))
    with caplog.at_level(logging.ERROR):
        logging.getLogger("positive.lifespan").error("leak %s", secret)
        assert secret in caplog.text
        caplog.clear()
        with TestClient(main.app) as client:
            assert_health(client)
        assert "DB_DISPOSE_FAILED" in caplog.text and secret not in caplog.text
        for record in caplog.records:
            assert record.args == () and record.exc_info is None and record.stack_info is None
            assert secret not in str(record.__dict__)
    assert engine.calls == 1 and main.app.state._dfm_db_resources is None


def test_startup_uses_fake_factory_without_dbapi_or_sql(environment, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("unexpected external I/O")
    engine = FakeEngine()
    monkeypatch.setattr(main, "_create_intermediate_resources", lambda config: (engine, object()))
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(forbidden))
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sqlalchemy, "create_engine", forbidden)
    with TestClient(main.app) as client:
        assert_health(client)
    assert engine.calls == 1


def test_import_does_not_read_credentials_or_create_engine(monkeypatch):
    original_getitem = type(os.environ).__getitem__
    def forbidden(*args, **kwargs): raise AssertionError("unexpected I/O")
    def read(environment, key):
        if key.startswith("DFM_DB_"): forbidden()
        return original_getitem(environment, key)
    alias = "_dataflow_lifespan_import_test"
    spec = importlib.util.spec_from_file_location(alias, main.__file__)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as guard:
        guard.setitem(sys.modules, alias, module)
        guard.setattr(type(os.environ), "__getitem__", read)
        guard.setattr(sqlalchemy, "create_engine", forbidden)
        guard.setattr(psycopg, "connect", forbidden)
        guard.setattr(socket.socket, "connect", forbidden)
        spec.loader.exec_module(module)
    assert alias not in sys.modules


def test_real_factory_creates_engine_without_physical_connect(environment, monkeypatch):
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append("unexpected network")
        raise AssertionError("unexpected physical connect")
    monkeypatch.setattr(session_module, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(forbidden))
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with TestClient(main.app) as client:
        resources = main.app.state._dfm_db_resources
        assert resources is not None
        assert resources[1].kw["autobegin"] is False
        assert_health(client)
    assert attempts == [] and main.app.state._dfm_db_resources is None
