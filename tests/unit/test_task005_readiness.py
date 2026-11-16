"""Tests of offline naming and fail-closed integration admission."""

from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import socket
from uuid import UUID, uuid4

import pytest


_spec = importlib.util.spec_from_file_location("task005_readiness", Path(__file__).parents[1] / "task005_readiness.py")
readiness = importlib.util.module_from_spec(_spec)
import sys
sys.modules[_spec.name] = readiness
try:
    _spec.loader.exec_module(readiness)
finally:
    del sys.modules[_spec.name]


def test_plan_is_per_run_and_immutable():
    first = readiness.plan_resources(uuid4(), excluded_resources=frozenset({"task004_database"}))
    second = readiness.plan_resources(uuid4(), excluded_resources=frozenset({"task004_database"}))
    assert first.database.startswith("task005_") and first.database != second.database
    assert len({first.database, first.preparation_role, first.test_role}) == 3
    assert all(len(value) <= 63 for value in (first.database, first.preparation_role, first.test_role))
    with pytest.raises(FrozenInstanceError): first.database = "production"


@pytest.mark.parametrize("bad_id,excluded", [
    (UUID(int=0), frozenset({"task004_database"})),
    ("not-a-uuid", frozenset({"task004_database"})),
    (uuid4(), frozenset()), (uuid4(), set()),
])
def test_rejects_missing_inventory_and_invalid_ids(bad_id, excluded):
    with pytest.raises(ValueError, match="^TASK005_PLAN_REJECTED$"):
        readiness.plan_resources(bad_id, excluded_resources=excluded)


@pytest.mark.parametrize("field", ["database", "preparation_role", "test_role"])
def test_excluded_resource_collision_rejected(field):
    run_id = uuid4()
    plan = readiness.plan_resources(run_id, excluded_resources=frozenset({"task004_database"}))
    with pytest.raises(ValueError, match="^TASK005_PLAN_REJECTED$"):
        readiness.plan_resources(run_id, excluded_resources=frozenset({getattr(plan, field)}))


def test_real_gate_is_fail_closed_without_network(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("unexpected network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with pytest.raises(RuntimeError, match="^TASK005_INTEGRATION_NOT_AUTHORIZED$"):
        readiness.require_real_integration_gate()
    with pytest.raises(AssertionError, match="unexpected network"):
        socket.socket.connect(None, ("synthetic", 0))


def test_all_owner_attestations_enable_preflight_only():
    readiness.require_real_integration_gate(opt_in=True, owner_approved=True,
                                            isolated_server=True, sql_approved=True,
                                            inventory_verified=True)


@pytest.mark.parametrize("missing", ["opt_in", "owner_approved", "isolated_server",
                                     "sql_approved", "inventory_verified"])
def test_every_attestation_is_required(missing):
    flags = dict(opt_in=True, owner_approved=True, isolated_server=True,
                 sql_approved=True, inventory_verified=True)
    flags[missing] = False
    with pytest.raises(RuntimeError, match="^TASK005_INTEGRATION_NOT_AUTHORIZED$"):
        readiness.require_real_integration_gate(**flags)


def test_fixture_preflight_calls_only_fake_factory():
    spec = importlib.util.spec_from_file_location("task005_fixture_offline", Path(__file__).parents[1] / "integration" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {"DFM_DB_HOST": "127.0.0.1", "DFM_DB_PORT": "5432", "DFM_DB_NAME": "task005_unique_123",
           "DFM_DB_USER": "task005_test_role", "DFM_DB_PASSWORD": "synthetic-secret",
           "DFM_DB_POOL_SIZE": "2", "DFM_DB_MAX_OVERFLOW": "1", "DFM_DB_POOL_TIMEOUT": "5",
           "DFM_DB_CONNECT_TIMEOUT": "3", "DFM_DB_POOL_PRE_PING": "true", "DFM_DB_POOL_RECYCLE": "1800",
           "DFM_DB_ISOLATION_LEVEL": "READ COMMITTED", "DFM_TEST_INTEGRATION": "YES",
           "DFM_TEST_OWNER_APPROVED": "YES", "DFM_TEST_ISOLATED_SERVER": "YES",
           "DFM_TEST_SQL_APPROVED": "YES", "DFM_TEST_INVENTORY_VERIFIED": "YES",
           "DFM_TEST_APPROVED_DATABASE": "task005_unique_123", "DFM_TEST_PREPARATION_ROLE": "task005_prep_role",
           "DFM_TEST_EXCLUDED_RESOURCES": "task004_database"}
    calls = []
    config, resources = module.prepare_resources(env, lambda value: calls.append(value) or ("fake-engine", "fake-maker"))
    assert calls == [config] and resources == ("fake-engine", "fake-maker")
    for change in ({"DFM_TEST_OWNER_APPROVED": "NO"}, {"PGHOSTADDR": ""},
                   {"DFM_TEST_APPROVED_DATABASE": "other"}, {"DFM_TEST_EXCLUDED_RESOURCES": ""}):
        bad = dict(env, **change)
        calls.clear()
        with pytest.raises((RuntimeError, ValueError)):
            module.prepare_resources(bad, lambda value: calls.append(value))
        assert calls == []

    class Result:
        def __init__(self, row): self.row = row
        def one(self): return self.row
    class Connection:
        def __init__(self, row): self.row = row
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, statement): return Result(self.row)
    class Engine:
        def __init__(self, row): self.row = row
        def connect(self): return Connection(self.row)
    expected = (config.name, config.user, 180006, "127.0.0.1", 5432)
    module.verify_identity(Engine(expected), config)
    with pytest.raises(RuntimeError, match="^TASK005_SERVER_IDENTITY_REJECTED$"):
        module.verify_identity(Engine(("other", *expected[1:])), config)

    class DisposeFailure:
        def dispose(self): raise ValueError("synthetic-dispose-secret")
    import traceback
    try:
        DisposeFailure().dispose()
    except ValueError as raw:
        assert "synthetic-dispose-secret" in "".join(traceback.format_exception(raw))
    with pytest.raises(RuntimeError, match="^TASK005_ENGINE_DISPOSE_FAILED$") as caught:
        module.dispose_safely(DisposeFailure())
    assert "synthetic-dispose-secret" not in str(caught.value)
    assert "synthetic-dispose-secret" not in "".join(traceback.format_exception(caught.value))


def test_integration_capture_excludes_pool_and_preserves_other_loggers(monkeypatch):
    import logging
    from _pytest.logging import catching_logs
    from dataflow_manager.db.session import _POOL_LOGGER_NAME
    spec = importlib.util.spec_from_file_location("task005_fixture_capture_test", Path(__file__).parents[1] / "integration" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_enter = catching_logs.__enter__
    module.pytest_configure(None)
    assert catching_logs.__enter__ is module._enter_without_pool_capture
    module.pytest_unconfigure(None)
    assert catching_logs.__enter__ is original_enter
    root = logging.getLogger()
    pool = logging.Logger(_POOL_LOGGER_NAME)
    pool.propagate = False
    other = logging.Logger("test.nonpropagating")
    other.propagate = False
    with monkeypatch.context() as guard:
        guard.setitem(root.manager.loggerDict, _POOL_LOGGER_NAME, pool)
        guard.setitem(root.manager.loggerDict, "test.nonpropagating", other)
        handler = logging.NullHandler()
        capture = catching_logs(handler)
        module._enter_without_pool_capture(capture)
        try:
            assert handler in root.handlers and handler in other.handlers
            assert handler not in pool.handlers
        finally:
            capture.__exit__(None, None, None)
        assert handler not in root.handlers and handler not in other.handlers
