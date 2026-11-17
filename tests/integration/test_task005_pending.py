"""Real PostgreSQL checks; require explicit fixture admission before DB I/O.

The owner must separately prepare task005_probe in the approved test database.
Committed probe rows are retained and inventoried, never deleted by these tests.
"""

from dataclasses import replace
import ctypes
import importlib.util
import os
from pathlib import Path
import select
import shlex
import time
from uuid import uuid4

import pytest
import psycopg
from psycopg import pq
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, TimeoutError

from dataflow_manager.db.session import _check_environment, _create_intermediate_resources, check_libpq_capability
from dataflow_manager.db.config import ConfigError
from dataflow_manager.db.uow import CleanupStatus, ErrorCode, UnitOfWork, UowError

_gate_spec = importlib.util.spec_from_file_location("_task005_test_gate", Path(__file__).with_name("conftest.py"))
_gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(_gate)


def _error_code_from_trace(trace):
    """Extract only C from a complete incoming libpq ErrorResponse frame."""
    try:
        if not trace.endswith(b"\n"):
            return None
        for line in trace.decode("utf-8").splitlines():
            tokens = shlex.split(line)
            if len(tokens) < 7 or tokens[2] != "B" or tokens[4] != "ErrorResponse":
                continue
            fields = tokens[5:]
            # libpq trace terminates an ErrorResponse with its zero field tag.
            if not line.rstrip().endswith(r"\x00") or not fields or fields[-1] != "x00":
                return None
            fields.pop()
            if len(fields) % 2:
                return None
            if any(len(key) != 1 for key in fields[::2]):
                return None
            codes = [value for key, value in zip(fields[::2], fields[1::2]) if key == "C"]
            return codes[0] if len(codes) == 1 else None
    except (UnicodeError, ValueError, IndexError):
        return None
    return None


def _trace_auth(config):
    """Trace one libpq handshake to bounded volatile memory; expose no raw trace."""
    import psycopg_binary
    _check_environment()
    check_libpq_capability()
    _gate._load("task005_readiness").require_safe_driver_logging()
    if pq.__impl__ != "binary":
        raise RuntimeError("TASK005_AUTH_TRACE_UNAVAILABLE")
    lib = ctypes.CDLL(str(Path(psycopg_binary.__file__).parent / ".dylibs" / "libpq.5.dylib"))
    libc = ctypes.CDLL(None)
    libc.fdopen.argtypes = [ctypes.c_int, ctypes.c_char_p]
    libc.fdopen.restype = ctypes.c_void_p
    libc.ferror.argtypes = [ctypes.c_void_p]
    libc.fclose.argtypes = [ctypes.c_void_p]
    lib.PQtrace.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.PQuntrace.argtypes = [ctypes.c_void_p]
    read_fd, write_fd = os.pipe()
    connection = stream = None
    traced = False
    try:
        os.set_blocking(write_fd, False)
        conninfo = psycopg.conninfo.make_conninfo(host=config.host, port=config.port,
            dbname=config.name, user=config.user, password=config.password,
            connect_timeout=config.connect_timeout, sslmode="disable", gssencmode="disable",
            require_auth="scram-sha-256")
        connection = pq.PGconn.connect_start(conninfo.encode())
        duplicate = os.dup(write_fd)
        stream = libc.fdopen(duplicate, b"w")
        if not stream:
            os.close(duplicate)
            raise RuntimeError("trace setup")
        # Disable stdio buffering so errors can be checked before detaching.
        libc.setvbuf.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        if libc.setvbuf(stream, None, 2, 0) != 0:
            raise RuntimeError("trace setup")
        lib.PQtrace(connection.pgconn_ptr, stream)
        traced = True
        deadline = time.monotonic() + 8
        while True:
            state = connection.connect_poll()
            if state in (pq.PollingStatus.OK, pq.PollingStatus.FAILED):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("trace timeout")
            if state == pq.PollingStatus.READING:
                select.select([connection.socket], [], [], remaining)
            elif state == pq.PollingStatus.WRITING:
                select.select([], [connection.socket], [], remaining)
        if libc.ferror(stream):
            raise RuntimeError("incomplete trace")
        lib.PQuntrace(connection.pgconn_ptr)
        traced = False
        libc.fclose(stream)
        stream = None
        os.close(write_fd)
        write_fd = None
        data = os.read(read_fd, 65536)
        return state, _error_code_from_trace(data)
    except Exception:
        raise RuntimeError("TASK005_AUTH_TRACE_FAILED") from None
    finally:
        if traced:
            lib.PQuntrace(connection.pgconn_ptr)
        if stream is not None:
            libc.fclose(stream)
        if connection is not None:
            connection.finish()
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_server_identity_auth_and_isolation(task005_real_integration):
    config, engine, maker = task005_real_integration
    assert check_libpq_capability().version > 0
    assert maker.kw["autobegin"] is False and maker.kw["close_resets_only"] is False
    assert engine.hide_parameters is True and not engine.echo
    with engine.connect() as connection:
        row = connection.execute(text("SELECT current_database(), current_user, "
                                      "current_setting('server_version_num')::integer, "
                                      "current_setting('transaction_isolation')")).one()
        assert row == (config.name, config.user, 180006, "read committed")
        assert connection.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()" )).scalar_one() is False


def test_wrong_password_is_rejected_client_side(task005_real_integration):
    # Startup failures can have no SQLSTATE in psycopg; this negative test alone
    # does not prove server-side authentication policy or absence of fallback.
    config, _, _ = task005_real_integration
    engine, _ = _create_intermediate_resources(replace(config, password=config.password + "_incorrect_probe"))
    try:
        _gate.protect_driver_logging(engine)
        with pytest.raises(DBAPIError) as caught:
            with engine.connect():
                pass
        state = getattr(caught.value.orig, "sqlstate", None)
        if state is None:
            status, state = _trace_auth(replace(config, password=config.password + "_incorrect_probe"))
            assert status == pq.PollingStatus.FAILED
            positive_status, positive_code = _trace_auth(config)
            assert positive_status == pq.PollingStatus.OK and positive_code is None
        assert state == "28P01"
    finally:
        try:
            engine.dispose()
        except Exception:
            raise RuntimeError("TASK005_ENGINE_DISPOSE_FAILED") from None
    fresh, _ = _create_intermediate_resources(config)
    try:
        _gate.protect_driver_logging(fresh)
        with fresh.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        try:
            fresh.dispose()
        except Exception:
            raise RuntimeError("TASK005_ENGINE_DISPOSE_FAILED") from None


def test_explicit_uow_commit_and_rollback(task005_real_integration):
    config, engine, maker = task005_real_integration
    created = []
    def capture():
        session = maker()
        created.append(session)
        return session
    committed, rolled_back = uuid4().hex, uuid4().hex
    _gate.record_probe(os.environ, config.name, committed, "intended")
    with UnitOfWork(capture) as work:
        created[-1].execute(text("INSERT INTO task005_probe (probe_id, value) VALUES (:id, :value)"),
                            {"id": committed, "value": "committed"})
        work.commit()
    _gate.record_probe(os.environ, config.name, committed, "committed")
    _gate.record_probe(os.environ, config.name, rolled_back, "intended")
    with UnitOfWork(capture):
        created[-1].execute(text("INSERT INTO task005_probe (probe_id, value) VALUES (:id, :value)"),
                            {"id": rolled_back, "value": "rollback"})
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT probe_id, value FROM task005_probe WHERE probe_id IN (:a, :b)"),
                                  {"a": committed, "b": rolled_back}).all()
    assert rows == [(committed, "committed")]


def test_read_committed_uses_independent_connections(task005_real_integration):
    config, engine, _ = task005_real_integration
    with engine.connect() as first, engine.connect() as second:
        first_pid = first.execute(text("SELECT pg_backend_pid()" )).scalar_one()
        second_pid = second.execute(text("SELECT pg_backend_pid()" )).scalar_one()
        assert first_pid != second_pid
        assert first.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        key = uuid4().hex
        _gate.record_probe(os.environ, config.name, key, "intended")
        first.execute(text("INSERT INTO task005_probe (probe_id, value) VALUES (:id, :value)"),
                      {"id": key, "value": "visibility"})
        query = text("SELECT count(*) FROM task005_probe WHERE probe_id = :id")
        assert second.execute(query, {"id": key}).scalar_one() == 0
        first.commit()
        _gate.record_probe(os.environ, config.name, key, "committed")
        assert second.execute(query, {"id": key}).scalar_one() == 1


def test_failed_transaction_is_rolled_back(task005_real_integration):
    config, engine, maker = task005_real_integration
    created = []
    def capture():
        session = maker()
        created.append(session)
        return session
    work = UnitOfWork(capture)
    probe = uuid4().hex
    _gate.record_probe(os.environ, config.name, probe, "intended")
    with pytest.raises(UowError) as caught:
        with work:
            created[-1].execute(text("INSERT INTO task005_probe (probe_id, value) VALUES (:id, :value)"),
                                {"id": probe, "value": "failed_transaction"})
            created[-1].execute(text("SELECT 1 / 0"))
    assert caught.value.code is ErrorCode.UOW_EXECUTION_FAILED
    assert work.outcome.rollback_status is CleanupStatus.SUCCEEDED
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM task005_probe WHERE probe_id = :id"),
                                  {"id": probe}).scalar_one() == 0


def test_pool_timeout_reuse_and_invalidation(task005_real_integration):
    _, engine, _ = task005_real_integration
    with engine.connect() as connection:
        pid = connection.execute(text("SELECT pg_backend_pid()" )).scalar_one()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT pg_backend_pid()" )).scalar_one() == pid
        connection.invalidate()
    with engine.connect() as replacement:
        assert replacement.execute(text("SELECT pg_backend_pid()" )).scalar_one() != pid
    checked_out = []
    try:
        for _ in range(3): checked_out.append(engine.connect())
        with pytest.raises(TimeoutError): engine.connect()
    finally:
        for connection in checked_out: connection.close()


def test_failed_pre_ping_replacement_and_pg_gate(task005_real_integration, monkeypatch):
    config, engine, _ = task005_real_integration
    attempts = []
    def observed(dialect, record, args, params): attempts.append("physical_connect")
    event.listen(engine, "do_connect", observed)
    try:
        with engine.connect() as observer:
            with engine.connect() as target:
                target_pid = target.execute(text("SELECT pg_backend_pid()" )).scalar_one()
                assert target_pid != observer.execute(text("SELECT pg_backend_pid()" )).scalar_one()
            row = observer.execute(text(
                "SELECT datname, usename, state, backend_type FROM pg_stat_activity "
                "WHERE pid = :pid"
            ), {"pid": target_pid}).one()
            assert tuple(row) == (config.name, config.user, "idle", "client backend")
            assert observer.execute(text("SELECT pg_terminate_backend(:pid)"),
                                    {"pid": target_pid}).scalar_one() is True
            deadline = time.monotonic() + 5
            while True:
                observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                remaining = observer.execute(text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"),
                                             {"pid": target_pid}).scalar_one()
                if remaining == 0:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.1)
            attempts.clear()
            with monkeypatch.context() as guard:
                guard.setenv("PG_TASK005_FORBIDDEN", "")
                with pytest.raises(ConfigError): engine.connect()
                assert attempts == []
            with engine.connect() as replacement:
                assert replacement.execute(text("SELECT pg_backend_pid()" )).scalar_one() != target_pid
                assert replacement.execute(text("SELECT 1")).scalar_one() == 1
            assert attempts == ["physical_connect"]
    finally:
        event.remove(engine, "do_connect", observed)


def test_lifespan_health_and_shutdown(task005_real_integration):
    from fastapi.testclient import TestClient
    from dataflow_manager.main import app
    with TestClient(app) as client:
        resources = app.state._dfm_db_resources
        assert resources is not None
        pool = resources[0].pool
        assert pool.checkedout() == 0
        response = client.get("/health")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert app.state._dfm_db_resources is None
    assert resources[0].pool is not pool


def test_dispose_releases_idle_physical_connection(task005_real_integration):
    _, engine, _ = task005_real_integration
    with engine.connect() as connection:
        old_pid = connection.execute(text("SELECT pg_backend_pid()" )).scalar_one()
    engine.dispose()
    deadline = time.monotonic() + 5
    with engine.connect() as observer:
        query = text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid")
        while observer.execute(query, {"pid": old_pid}).scalar_one():
            assert time.monotonic() < deadline
            time.sleep(0.1)
