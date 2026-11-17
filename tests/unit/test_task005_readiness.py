"""Tests of offline naming and fail-closed integration admission."""

from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import socket
from uuid import UUID, uuid4

import pytest


def test_auth_trace_parser_rejects_spoofed_frames_and_fields():
    spec = importlib.util.spec_from_file_location("auth_trace_unit", Path(__file__).parents[1] / "integration" / "test_task005_pending.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parse = module._error_code_from_trace
    assert parse(b'2026-01-01 12:00:00 B 100 ErrorResponse S "FATAL" C "28P01" M "redacted" \\x00\n') == "28P01"
    assert parse(b'2026-01-01 12:00:00 B 100 ErrorResponse S "FATAL" C "28P01" M "redacted"\n') is None
    assert parse(b'2026-01-01 12:00:00 B 100 ErrorResponse S "FATAL" C "28P01" M "redacted" \\x00') is None
    assert parse(b'2026-01-01 12:00:00 B 100 NoticeResponse M "ErrorResponse" C "28P01"\n') is None
    assert parse(b'2026-01-01 12:00:00 B 100 ErrorResponse M "text C \\"28P01\\""\n') is None
    assert parse(b'2026-01-01 12:00:00 F 100 ErrorResponse C "28P01"\n') is None
    assert parse(b'2026-01-01 12:00:00 B 100 ErrorResponse C "28000" \\x00\n') == "28000"
    assert parse(b'2026-01-01 12:00:00 B 100 ErrorResponse C "28P01" C "28000"\n') is None


@pytest.mark.parametrize("fault", ["poll", "ferror"])
def test_auth_trace_cleanup_on_failure(monkeypatch, fault):
    """A traced PGconn must be detached before its FILE and PGconn are closed."""
    import os
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("auth_trace_cleanup", Path(__file__).parents[1] / "integration" / "test_task005_pending.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class Function:
        def __init__(self, fn): self.fn = fn
        def __call__(self, *args): return self.fn(*args)

    def close_stream(fd):
        calls.append("fclose")
        os.close(fd)
        return 0

    libc = SimpleNamespace(fdopen=Function(lambda fd, mode: fd),
        setvbuf=Function(lambda *args: 0),
        ferror=Function(lambda stream: int(fault == "ferror")),
        fclose=Function(close_stream))
    lib = SimpleNamespace(PQtrace=Function(lambda *args: calls.append("trace")),
        PQuntrace=Function(lambda *args: calls.append("untrace")))

    class Connection:
        pgconn_ptr = 1
        def connect_poll(self):
            if fault == "poll":
                raise TimeoutError("synthetic timeout")
            return 0
        def finish(self): calls.append("finish")

    class PGconn:
        @staticmethod
        def connect_start(conninfo): return Connection()

    monkeypatch.setattr(module, "pq", SimpleNamespace(__impl__="binary", PGconn=PGconn,
        PollingStatus=SimpleNamespace(OK=3, FAILED=0, READING=1, WRITING=2)))
    monkeypatch.setattr(module.ctypes, "CDLL", lambda path: libc if path is None else lib)
    monkeypatch.setattr(module, "_check_environment", lambda: None)
    monkeypatch.setattr(module, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(module._gate, "_load", lambda name: SimpleNamespace(require_safe_driver_logging=lambda: None))
    monkeypatch.setattr(module.psycopg.conninfo, "make_conninfo", lambda **kwargs: "safe fake")
    config = SimpleNamespace(host="127.0.0.1", port=5432, name="synthetic", user="synthetic",
        password="synthetic-secret", connect_timeout=3)
    with pytest.raises(RuntimeError, match="^TASK005_AUTH_TRACE_FAILED$"):
        module._trace_auth(config)
    assert calls == ["trace", "untrace", "fclose", "finish"]


def test_auth_trace_rejects_unsupported_implementation_before_network(monkeypatch):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("auth_trace_impl", Path(__file__).parents[1] / "integration" / "test_task005_pending.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_check_environment", lambda: None)
    monkeypatch.setattr(module, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(module._gate, "_load", lambda name: SimpleNamespace(require_safe_driver_logging=lambda: None))
    monkeypatch.setattr(module, "pq", SimpleNamespace(__impl__="python"))
    with pytest.raises(RuntimeError, match="^TASK005_AUTH_TRACE_UNAVAILABLE$"):
        module._trace_auth(None)


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


def test_psycopg_debug_diagnostics_fail_closed(monkeypatch):
    import logging
    logger = logging.getLogger("psycopg")
    monkeypatch.setattr(logger, "isEnabledFor", lambda level: level == logging.DEBUG)
    with pytest.raises(RuntimeError, match="^TASK005_DRIVER_LOGGING_UNSAFE$"):
        readiness.require_safe_driver_logging()
    monkeypatch.setattr(logger, "isEnabledFor", lambda level: False)
    readiness.require_safe_driver_logging()


@pytest.mark.parametrize("missing", ["opt_in", "owner_approved", "isolated_server",
                                     "sql_approved", "inventory_verified"])
def test_every_attestation_is_required(missing):
    flags = dict(opt_in=True, owner_approved=True, isolated_server=True,
                 sql_approved=True, inventory_verified=True)
    flags[missing] = False
    with pytest.raises(RuntimeError, match="^TASK005_INTEGRATION_NOT_AUTHORIZED$"):
        readiness.require_real_integration_gate(**flags)


def test_auth_trace_closes_pipe_when_nonblocking_setup_fails(monkeypatch):
    import os
    spec = importlib.util.spec_from_file_location("auth_trace_fd_test", Path(__file__).parents[1] / "integration" / "test_task005_pending.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_check_environment", lambda: None)
    monkeypatch.setattr(module, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(module._gate, "_load", lambda _: type("Guard", (), {"require_safe_driver_logging": staticmethod(lambda: None)})())
    descriptors = []
    real_pipe, real_close = os.pipe, os.close
    def pipe():
        fds = real_pipe()
        descriptors.extend(fds)
        return fds
    closed = []
    def close(fd):
        closed.append(fd)
        real_close(fd)
    monkeypatch.setattr(module.os, "pipe", pipe)
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module.os, "set_blocking", lambda *args: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(RuntimeError, match="^TASK005_AUTH_TRACE_FAILED$"):
        module._trace_auth(None)
    assert sorted(closed) == sorted(descriptors) and len(descriptors) == 2


def test_fixture_preflight_calls_only_fake_factory():
    spec = importlib.util.spec_from_file_location("task005_fixture_offline", Path(__file__).parents[1] / "integration" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run_id = "12345678123442349234123456789abc"
    env = {"DFM_DB_HOST": "127.0.0.1", "DFM_DB_PORT": "5432", "DFM_DB_NAME": "task005_" + run_id,
           "DFM_DB_USER": "task005_test_" + run_id, "DFM_DB_PASSWORD": "synthetic-secret",
           "DFM_DB_POOL_SIZE": "2", "DFM_DB_MAX_OVERFLOW": "1", "DFM_DB_POOL_TIMEOUT": "5",
           "DFM_DB_CONNECT_TIMEOUT": "3", "DFM_DB_POOL_PRE_PING": "true", "DFM_DB_POOL_RECYCLE": "1800",
           "DFM_DB_ISOLATION_LEVEL": "READ COMMITTED", "DFM_TEST_INTEGRATION": "YES",
           "DFM_TEST_OWNER_APPROVED": "YES", "DFM_TEST_ISOLATED_SERVER": "YES",
           "DFM_TEST_SQL_APPROVED": "YES", "DFM_TEST_INVENTORY_VERIFIED": "YES",
           "DFM_TEST_APPROVED_DATABASE": "task005_" + run_id, "DFM_TEST_PREPARATION_ROLE": "task005_prep_" + run_id,
           "DFM_TEST_APPROVED_TEST_ROLE": "task005_test_" + run_id, "DFM_TEST_RUN_ID": run_id,
           "DFM_TEST_EXPECTED_SERVER_ADDRESS": "172.17.0.2",
           "DFM_TEST_LEDGER_PATH": str(Path(__file__).parents[1] / "integration" / ("task005_ledger_" + run_id + ".jsonl")),
           "DFM_TEST_EXCLUDED_RESOURCES": "task004_database"}
    calls = []
    config, resources = module.prepare_resources(env, lambda value: calls.append(value) or ("fake-engine", "fake-maker"))
    assert calls == [config] and resources == ("fake-engine", "fake-maker")
    for change in ({"DFM_TEST_OWNER_APPROVED": "NO"}, {"PGHOSTADDR": ""},
                   {"DFM_TEST_APPROVED_TEST_ROLE": "postgres"},
                   {"DFM_TEST_APPROVED_DATABASE": "other"}, {"DFM_TEST_EXCLUDED_RESOURCES": ""},
                   {"DFM_TEST_EXPECTED_SERVER_ADDRESS": "not-an-address"}):
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
    expected = (config.name, config.user, 180006, "172.17.0.2", 5432)
    module.verify_identity(Engine(expected), config, env)
    with pytest.raises(RuntimeError, match="^TASK005_SERVER_IDENTITY_REJECTED$"):
        module.verify_identity(Engine(("other", *expected[1:])), config, env)
    with pytest.raises(RuntimeError, match="^TASK005_SERVER_IDENTITY_REJECTED$"):
        module.verify_identity(Engine((*expected[:3], "127.0.0.1", expected[4])), config, env)

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


def test_integration_capture_observes_sanitized_pool_diagnostics(monkeypatch):
    import logging
    from _pytest.logging import catching_logs
    from dataflow_manager.db import session

    spec = importlib.util.spec_from_file_location("task005_fixture_capture_observation", Path(__file__).parents[1] / "integration" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Recorder(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []
        def emit(self, record):
            self.records.append(record)

    secret = "synthetic-pool-secret-control"
    root = logging.getLogger()
    pool = logging.Logger(session._POOL_LOGGER_NAME)
    recorder = Recorder()
    original_enter = catching_logs.__enter__
    with monkeypatch.context() as guard:
        guard.setitem(root.manager.loggerDict, session._POOL_LOGGER_NAME, pool)
        guard.setattr(session, "_pool_handler", None)
        session._configure_pool_diagnostics()
        module.pytest_configure(None)
        try:
            with catching_logs(recorder, level=logging.DEBUG):
                logging.getLogger("task005-positive-control").warning("%s", secret)
                assert secret in recorder.records[-1].getMessage()
                recorder.records.clear()
                assert pool.handlers == [session._pool_handler]
                pool.error("%s", secret, exc_info=True, stack_info=True)
                records = [r for r in recorder.records if r.name == "dataflow_manager.db.pool_diagnostics"]
                assert len(records) == 1
                record = records[0]
                assert record.msg == "Pool diagnostic error" and record.category == "POOL_DIAGNOSTIC_ERROR"
                assert not record.args and record.exc_info is None and record.stack_info is None
                assert all(secret not in r.getMessage() for r in recorder.records)
                assert all(r.name != session._POOL_LOGGER_NAME for r in recorder.records)
        finally:
            module.pytest_unconfigure(None)
        assert catching_logs.__enter__ is original_enter


def test_migration_runner_gates_before_factory_and_passes_connection(monkeypatch):
    from sqlalchemy import event
    spec = importlib.util.spec_from_file_location("task005_migration_runner_test", Path(__file__).parents[1] / "task005_migration_runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    run_id = "12345678123442349234123456789abc"
    env = {"DFM_DB_HOST": "127.0.0.1", "DFM_DB_PORT": "5432", "DFM_DB_NAME": "task005_" + run_id,
           "DFM_DB_USER": "task005_test_" + run_id, "DFM_DB_PASSWORD": "synthetic-secret",
           "DFM_DB_POOL_SIZE": "2", "DFM_DB_MAX_OVERFLOW": "1", "DFM_DB_POOL_TIMEOUT": "5",
           "DFM_DB_CONNECT_TIMEOUT": "3", "DFM_DB_POOL_PRE_PING": "true", "DFM_DB_POOL_RECYCLE": "1800",
           "DFM_DB_ISOLATION_LEVEL": "READ COMMITTED", "DFM_TEST_INTEGRATION": "YES",
           "DFM_TEST_OWNER_APPROVED": "YES", "DFM_TEST_ISOLATED_SERVER": "YES",
           "DFM_TEST_SQL_APPROVED": "YES", "DFM_TEST_INVENTORY_VERIFIED": "YES",
           "DFM_TEST_APPROVED_DATABASE": "task005_" + run_id, "DFM_TEST_APPROVED_TEST_ROLE": "task005_test_" + run_id,
           "DFM_TEST_PREPARATION_ROLE": "task005_prep_" + run_id, "DFM_TEST_EXCLUDED_RESOURCES": "task004_database",
           "DFM_TEST_RUN_ID": run_id,
           "DFM_TEST_EXPECTED_SERVER_ADDRESS": "172.17.0.2",
           "DFM_TEST_LEDGER_PATH": str(Path(__file__).parents[1] / "integration" / ("task005_ledger_" + run_id + ".jsonl"))}
    calls = []
    def factory(config): calls.append(config); return FakeEngine(), object()
    with pytest.raises(RuntimeError, match="TASK005_MIGRATION_NOT_AUTHORIZED"):
        runner.migrate(env, "synthetic-prep-secret", factory=factory, upgrade=lambda *args: None)
    assert calls == []
    env.update(DFM_TEST_MIGRATIONS_APPROVED="YES", DFM_TEST_DROP_CONSTRAINT_APPROVED="YES")

    class FakeResult:
        def one(self): return ("task005_" + run_id, "task005_prep_" + run_id, 180006, "172.17.0.2", 5432)
    class FakeConnection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, statement): return FakeResult()
    class FakeEngine:
        def connect(self): return FakeConnection()
        def begin(self): return FakeConnection()
        def dispose(self): calls.append("disposed")
    def upgrade(alembic, revision):
        assert isinstance(alembic.attributes["connection"], FakeConnection)
        assert revision == "0002_host_policy"
        calls.append("upgrade")
    original_listen = event.listen
    def listen(target, *args, **kwargs):
        if isinstance(target, FakeEngine):
            calls.append("guarded")
            return None
        return original_listen(target, *args, **kwargs)
    monkeypatch.setattr(event, "listen", listen)
    runner.migrate(env, "synthetic-prep-secret", factory=factory, upgrade=upgrade)
    assert calls[0].user == "task005_prep_" + run_id and calls[0].name == "task005_" + run_id
    assert calls[1:] == ["guarded", "upgrade", "disposed"]


def test_probe_ledger_rejects_unsafe_paths_and_records_only_fixed_fields(monkeypatch):
    spec = importlib.util.spec_from_file_location("task005_ledger_test", Path(__file__).parents[1] / "integration" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = Path(__file__).parents[1] / "integration" / "task005_ledger_synthetic.jsonl"
    env = {"DFM_TEST_LEDGER_PATH": str(path), "DFM_TEST_APPROVED_DATABASE": "task005_unique_123"}
    key = uuid4().hex
    writes = []
    with monkeypatch.context() as guard:
        guard.setattr(module.os, "open", lambda *args: 123)
        guard.setattr(module.os, "write", lambda fd, data: writes.append(data) or len(data))
        guard.setattr(module.os, "close", lambda fd: None)
        module.record_probe(env, "task005_unique_123", key, "intended")
        module.record_probe(env, "task005_unique_123", key, "committed")
    assert len(writes) == 2 and all(key.encode() in item for item in writes)
    assert all(b"password" not in item for item in writes)
    with pytest.raises(RuntimeError, match="^TASK005_LEDGER_FAILED$"):
        module.record_probe(env, "task005_unique_123", "not-a-uuid", "committed")
    with pytest.raises(RuntimeError, match="^TASK005_LEDGER_REJECTED$"):
        module.ledger_path({"DFM_TEST_LEDGER_PATH": "/tmp/task005_ledger_bad.jsonl"})
