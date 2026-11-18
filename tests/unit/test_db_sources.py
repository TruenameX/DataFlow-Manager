from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from dataflow_manager.db.sources import SourceNameConflict, SourceRepository, _name_conflict
from dataflow_manager.db.uow import ErrorCode, LifecycleState, UnitOfWork, UowError


class FakeSession:
    def __init__(self):
        self.calls = []
        self.item = None
        self.error = None

    def begin(self): self.calls.append("begin")
    def commit(self): self.calls.append("commit")
    def rollback(self): self.calls.append("rollback")
    def close(self): self.calls.append("close")
    def add(self, item): self.calls.append("add"); self.item = item
    def delete(self, item): self.calls.append("delete"); self.item = None
    def flush(self):
        self.calls.append("flush")
        if self.error:
            raise self.error
    def get(self, model, key):
        self.calls.append("get")
        if self.error: raise self.error
        return self.item
    def scalars(self, statement):
        self.calls.append("scalars")
        self.last_statement = statement
        if self.error: raise self.error
        return SimpleNamespace(first=lambda: self.item, all=lambda: [] if self.item is None else [self.item])


def _integrity(code="23505", constraint="uq_data_sources_name_key", schema="public", table="data_sources"):
    class DbError(Exception):
        sqlstate = code
        diag = SimpleNamespace(constraint_name=constraint, schema_name=schema, table_name=table)
    return IntegrityError("SQL synthetic secret", {"value": "secret"}, DbError("secret"))


@pytest.mark.parametrize("error, expected", [
    (_integrity(), True),
    (_integrity(schema=None, table=None), True),
    (_integrity(code="23514"), False),
    (_integrity(constraint="other"), False),
    (_integrity(schema="other"), False),
    (_integrity(table="other"), False),
])
def test_conflict_classification_is_structural(error, expected):
    assert _name_conflict(error) is expected


def test_crud_uses_caller_uow_and_preserves_noop_and_token():
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    repo = SourceRepository(uow)
    with uow:
        item = repo.create(name="Name", host="localhost", port=5432, database_name="db",
                           username="user", password_encrypted="token")
        assert repo.get(uuid4()) is item
        assert repo.list() == [item]
        before = list(session.calls)
        assert repo.update(uuid4(), {"name": "Name"}) is item
        assert session.calls == before + ["scalars"]
        assert item.password_encrypted == "token"
        repo.update(uuid4(), {"host": "example.test"})
        assert item.password_encrypted == "token" and item.host == "example.test"
        count = session.calls.count("flush")
        repo.update(uuid4(), {"password_encrypted": "token"})
        assert session.calls.count("flush") == count + 1
        assert repo.delete(uuid4()) is True
        assert repo.delete(uuid4()) is False
        assert repo.get(uuid4()) is None and repo.list() == []
        assert "commit" not in session.calls
        uow.commit()
    assert session.calls[-2:] == ["commit", "close"]


@pytest.mark.parametrize("error, expected_type", [(_integrity(), SourceNameConflict),
                                                   (_integrity(code="23514"), UowError),
                                                   (RuntimeError("secret"), UowError)])
def test_caught_db_failure_marks_failed_and_forbids_commit(error, expected_type):
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    repo = SourceRepository(uow)
    with pytest.raises(UowError) as final:
        with uow:
            session.error = error
            with pytest.raises(expected_type) as preliminary:
                repo.get(uuid4())
            assert "secret" not in str(preliminary.value) and "secret" not in repr(preliminary.value)
            assert uow.state is LifecycleState.FAILED
            with pytest.raises(UowError): uow.commit()
    assert final.value.outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED
    assert "commit" not in session.calls and session.calls[-2:] == ["rollback", "close"]


def test_reuse_is_guarded_and_unsafe_update_fields_rejected():
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    repo = SourceRepository(uow)
    with pytest.raises(UowError): repo.get(uuid4())
    with uow:
        with pytest.raises(ValueError): repo.update(uuid4(), {"updated_at": "secret"})
    with pytest.raises(UowError): repo.get(uuid4())
    assert "get" not in session.calls


def test_locked_update_reuses_one_select_and_guards_other_objects():
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    with uow:
        repo = SourceRepository(uow)
        source = repo.create(name="Name", host="localhost", port=5432, database_name="db",
                             username="user", password_encrypted="token")
        locked = repo._get_for_update(uuid4())
        assert locked is source
        assert session.last_statement._for_update_arg is not None
        assert session.last_statement.get_execution_options()["populate_existing"] is True
        with pytest.raises(ValueError, match="source is not locked"):
            repo._update_locked(object(), {"host": "other"})
        before = session.calls.count("scalars")
        repo._update_locked(locked, {"host": "example.test"})
        assert session.calls.count("scalars") == before
        assert source.host == "example.test"
        uow.commit()


def test_broken_conflict_diagnostics_remain_safe():
    class BrokenError(Exception):
        @property
        def diag(self):
            raise RuntimeError("synthetic-secret-diagnostic")
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    with pytest.raises(UowError):
        with uow:
            session.error = IntegrityError("secret sql", {"token": "secret"}, BrokenError())
            with pytest.raises(UowError) as caught:
                SourceRepository(uow).get(uuid4())
            assert type(caught.value) is UowError
            assert "secret" not in repr(caught.value) + str(caught.value)
            assert uow.state is LifecycleState.FAILED


def test_interrupted_repository_operation_is_preserved_if_caught():
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    interrupted = KeyboardInterrupt("synthetic")
    with pytest.raises(KeyboardInterrupt) as caught:
        with uow:
            session.error = interrupted
            try:
                SourceRepository(uow).get(uuid4())
            except KeyboardInterrupt:
                pass
            assert uow.state is LifecycleState.FAILED
            with pytest.raises(UowError):
                uow.commit()
    assert caught.value is interrupted
    assert uow.outcome.primary_error_code is ErrorCode.UOW_INVALID_STATE
    assert session.calls[-2:] == ["rollback", "close"]


def test_diagnostic_interrupt_does_not_replace_recorded_error():
    interrupted = KeyboardInterrupt("synthetic")
    class BrokenError(Exception):
        @property
        def diag(self):
            raise interrupted
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    with pytest.raises(KeyboardInterrupt) as caught:
        with uow:
            session.error = IntegrityError("secret", {}, BrokenError())
            try:
                SourceRepository(uow).get(uuid4())
            except KeyboardInterrupt:
                pass
    assert caught.value is interrupted
    assert uow.outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED


def test_conflict_fact_survives_as_explicit_service_classification():
    session = FakeSession()
    uow = UnitOfWork(lambda: session)
    conflict_seen = False
    with pytest.raises(UowError) as final:
        with uow:
            session.error = _integrity()
            try:
                SourceRepository(uow).get(uuid4())
            except SourceNameConflict:
                conflict_seen = True
    assert conflict_seen
    assert final.value.outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED
