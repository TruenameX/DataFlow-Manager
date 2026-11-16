"""Real PostgreSQL checks; require explicit fixture admission before DB I/O.

The owner must separately prepare task005_probe in the approved test database.
Committed probe rows are retained and inventoried, never deleted by these tests.
"""

from dataclasses import replace
import time
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, TimeoutError

from dataflow_manager.db.session import _create_intermediate_resources, check_libpq_capability
from dataflow_manager.db.uow import CleanupStatus, ErrorCode, UnitOfWork, UowError


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
        with pytest.raises(DBAPIError) as caught:
            with engine.connect():
                pass
        assert getattr(caught.value.orig, "sqlstate", None) in (None, "28P01")
    finally:
        try:
            engine.dispose()
        except Exception:
            raise RuntimeError("TASK005_ENGINE_DISPOSE_FAILED") from None
    fresh, _ = _create_intermediate_resources(config)
    try:
        with fresh.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        try:
            fresh.dispose()
        except Exception:
            raise RuntimeError("TASK005_ENGINE_DISPOSE_FAILED") from None


def test_explicit_uow_commit_and_rollback(task005_real_integration):
    _, engine, maker = task005_real_integration
    created = []
    def capture():
        session = maker()
        created.append(session)
        return session
    committed, rolled_back = uuid4().hex, uuid4().hex
    with UnitOfWork(capture) as work:
        created[-1].execute(text("INSERT INTO task005_probe (probe_id, value) VALUES (:id, :value)"),
                            {"id": committed, "value": "committed"})
        work.commit()
    with UnitOfWork(capture):
        created[-1].execute(text("INSERT INTO task005_probe (probe_id, value) VALUES (:id, :value)"),
                            {"id": rolled_back, "value": "rollback"})
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT probe_id, value FROM task005_probe WHERE probe_id IN (:a, :b)"),
                                  {"a": committed, "b": rolled_back}).all()
    assert rows == [(committed, "committed")]


def test_read_committed_uses_independent_connections(task005_real_integration):
    _, engine, _ = task005_real_integration
    with engine.connect() as first, engine.connect() as second:
        first_pid = first.execute(text("SELECT pg_backend_pid()" )).scalar_one()
        second_pid = second.execute(text("SELECT pg_backend_pid()" )).scalar_one()
        assert first_pid != second_pid
        assert first.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        key = uuid4().hex
        first.execute(text("INSERT INTO task005_probe (probe_id, value) VALUES (:id, :value)"),
                      {"id": key, "value": "visibility"})
        query = text("SELECT count(*) FROM task005_probe WHERE probe_id = :id")
        assert second.execute(query, {"id": key}).scalar_one() == 0
        first.commit()
        assert second.execute(query, {"id": key}).scalar_one() == 1


def test_failed_transaction_is_rolled_back(task005_real_integration):
    _, engine, maker = task005_real_integration
    created = []
    def capture():
        session = maker()
        created.append(session)
        return session
    work = UnitOfWork(capture)
    with pytest.raises(UowError) as caught:
        with work:
            created[-1].execute(text("SELECT 1 / 0"))
    assert caught.value.code is ErrorCode.UOW_EXECUTION_FAILED
    assert work.outcome.rollback_status is CleanupStatus.SUCCEEDED
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


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
