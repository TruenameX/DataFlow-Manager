from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import psycopg
from sqlalchemy.exc import IntegrityError, OperationalError

import dataflow_manager.services.sources as services
from dataflow_manager.db.sources import SourceNameConflict
from dataflow_manager.db.uow import ErrorCode, LifecycleState
from dataflow_manager.security.crypto import CryptoError, CryptoErrorCode


class Session:
    def __init__(self, *, commit_error=False, close_error=False):
        self.calls = []
        self.commit_error = commit_error
        self.close_error = close_error

    def begin(self): self.calls.append("begin")
    def commit(self):
        self.calls.append("commit")
        if self.commit_error: raise RuntimeError("secret-commit")
    def rollback(self): self.calls.append("rollback")
    def close(self):
        self.calls.append("close")
        if self.close_error: raise RuntimeError("secret-close")


class Crypto:
    def encrypt(self, value): return "cipher:" + value


def item():
    return SimpleNamespace(id=uuid4(), name="name", host="localhost", port=5432,
                           database_name="db", username="user", password_encrypted="secret-token",
                           created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))


def test_create_snapshot_only_after_commit_and_close(monkeypatch):
    session = Session()
    source = item()
    class Repo:
        def __init__(self, work): self.work = work
        def create(self, **fields):
            assert fields["password_encrypted"] == "cipher:synthetic-password"
            assert "password" not in fields
            assert self.work.state is LifecycleState.ACTIVE
            return source
    monkeypatch.setattr(services, "SourceRepository", Repo)
    result = services.SourceService(lambda: session, Crypto()).create(
        dict(name="name", host="localhost", port=5432, database="db", username="user",
             password="synthetic-password"))
    assert session.calls == ["begin", "commit", "close"]
    assert result.id == source.id and result.database == "db"
    assert "password" not in repr(result) and "secret-token" not in repr(result)


@pytest.mark.parametrize("method", ["get", "update", "delete"])
def test_missing_is_not_database_failure(monkeypatch, method):
    session = Session()
    class Repo:
        def __init__(self, _): pass
        def get(self, _): return None
        def _get_for_update(self, _): return None
        def delete(self, _): return False
    monkeypatch.setattr(services, "SourceRepository", Repo)
    service = services.SourceService(lambda: session, None)
    args = (uuid4(), {"host": "x"}) if method == "update" else (uuid4(),)
    with pytest.raises(services.ServiceError) as error:
        getattr(service, method)(*args)
    assert error.value.code == "source_not_found"
    assert session.calls == ["begin", "rollback", "close"]


def test_no_crypto_for_read_delete_or_patch_without_password(monkeypatch):
    session = Session()
    source = item()
    received = []
    class Repo:
        def __init__(self, _): pass
        def get(self, _): return source
        def list(self): return [source]
        def _get_for_update(self, _): return source
        def _update_locked(self, _, changes): received.append(changes); return source
        def delete(self, _): return True
    monkeypatch.setattr(services, "SourceRepository", Repo)
    service = services.SourceService(lambda: session, None)
    assert service.get(uuid4()).id == source.id
    assert service.list()[0].id == source.id
    assert service.update(uuid4(), {"database": "new"}).id == source.id
    assert received == [{"database_name": "new"}]
    assert service.delete(uuid4()) is None
    with pytest.raises(services.ServiceError, match="operation_unavailable"):
        service.update(uuid4(), {"password": "secret"})


@pytest.mark.parametrize("crypto", [None, Crypto()])
def test_missing_patch_with_password_is_not_masked_by_missing_key(monkeypatch, crypto):
    session = Session()
    class SpyCrypto:
        def encrypt(self, _): pytest.fail("crypto must not run for missing source")
    class Repo:
        def __init__(self, _): pass
        def _get_for_update(self, _): return None
    monkeypatch.setattr(services, "SourceRepository", Repo)
    with pytest.raises(services.ServiceError) as error:
        services.SourceService(lambda: session, SpyCrypto() if crypto else None).update(uuid4(), {"password": "synthetic"})
    assert error.value.code == "source_not_found"
    assert session.calls == ["begin", "rollback", "close"]


@pytest.mark.parametrize("crypto,expected", [(None, "operation_unavailable"), ("broken", "internal_error")])
def test_patch_crypto_failure_after_lock_rolls_back(monkeypatch, crypto, expected):
    session = Session()
    calls = []
    source = item()
    class Repo:
        def __init__(self, _): pass
        def _get_for_update(self, _): calls.append("lock"); return source
        def _update_locked(self, *_): pytest.fail("failed crypto must not update")
    class BrokenCrypto:
        def encrypt(self, _): calls.append("encrypt"); raise CryptoError(CryptoErrorCode.ENCRYPTION_FAILED)
    monkeypatch.setattr(services, "SourceRepository", Repo)
    with pytest.raises(services.ServiceError) as error:
        services.SourceService(lambda: session, BrokenCrypto() if crypto else None).update(
            uuid4(), {"password": "synthetic"})
    assert error.value.code == expected
    assert calls == (["lock", "encrypt"] if crypto else ["lock"])
    assert session.calls == ["begin", "rollback", "close"]


def test_patch_locks_then_encrypts_and_updates_in_one_uow(monkeypatch):
    session = Session()
    calls = []
    source = item()
    class Repo:
        def __init__(self, work): self.work = work
        def _get_for_update(self, _): calls.append("lock"); return source
        def _update_locked(self, locked, changes):
            assert locked is source and changes == {"password_encrypted": "cipher:synthetic"}
            assert self.work.state is LifecycleState.ACTIVE
            calls.append("update")
            return source
    class SpyCrypto:
        def encrypt(self, value): calls.append("encrypt"); return "cipher:" + value
    monkeypatch.setattr(services, "SourceRepository", Repo)
    result = services.SourceService(lambda: session, SpyCrypto()).update(uuid4(), {"password": "synthetic"})
    assert result.id == source.id and calls == ["lock", "encrypt", "update"]
    assert session.calls == ["begin", "commit", "close"]


def test_conflict_fact_survives_final_uow_and_cleanup(monkeypatch):
    session = Session(close_error=True)
    class Repo:
        def __init__(self, work): self.work = work
        def create(self, **_):
            self.work._record(ErrorCode.UOW_EXECUTION_FAILED)
            self.work._state = LifecycleState.FAILED
            raise SourceNameConflict()
    monkeypatch.setattr(services, "SourceRepository", Repo)
    service = services.SourceService(lambda: session, Crypto())
    with pytest.raises(services.ServiceError) as caught:
        service.create(dict(name="n", host="h", port=1, database="d", username="u", password="secret"))
    assert caught.value.code == "source_name_conflict"
    assert session.calls == ["begin", "rollback", "close"]


@pytest.mark.parametrize("commit_error,close_error,expected", [(True, False, "internal_error"),
                                                               (False, True, "internal_error")])
def test_commit_uncertainty_and_post_commit_cleanup_are_not_success(monkeypatch, commit_error, close_error, expected):
    session = Session(commit_error=commit_error, close_error=close_error)
    class Repo:
        def __init__(self, _): pass
        def create(self, **_): return item()
    monkeypatch.setattr(services, "SourceRepository", Repo)
    service = services.SourceService(lambda: session, Crypto())
    with pytest.raises(services.ServiceError) as caught:
        service.create(dict(name="n", host="h", port=1, database="d", username="u", password="secret"))
    assert caught.value.code == expected
    assert session.calls.count("commit") == 1
    assert ("rollback" in session.calls) is commit_error
    assert "secret" not in str(caught.value) + repr(caught.value)


def test_unavailable_resources_fail_closed():
    with pytest.raises(services.ServiceError) as caught:
        services.SourceService(None, None).list()
    assert caught.value.code == "operation_unavailable"


def test_unexpected_crypto_failure_is_internal_error():
    class BrokenCrypto:
        def encrypt(self, _): raise CryptoError(CryptoErrorCode.ENCRYPTION_FAILED)
    with pytest.raises(services.ServiceError) as caught:
        services.SourceService(lambda: Session(), BrokenCrypto()).create(
            dict(name="n", host="h", port=1, database="d", username="u", password="secret"))
    assert caught.value.code == "internal_error"


@pytest.mark.parametrize("db_error,expected", [
    (OperationalError("synthetic-sql", {"password": "secret"}, Exception("secret")), "internal_error"),
    (OperationalError("synthetic-sql", {}, psycopg.OperationalError()), "operation_unavailable"),
    (OperationalError("synthetic-sql", {}, psycopg.errors.DeadlockDetected()), "internal_error"),
    (OperationalError("synthetic-sql", {}, psycopg.errors.SerializationFailure()), "internal_error"),
    (IntegrityError("synthetic-sql", {"password": "secret"}, Exception("secret")), "internal_error"),
])
def test_structural_db_failure_classification_is_safe(db_error, expected):
    class FailingSession(Session):
        def get(self, *_): raise db_error
    session = FailingSession()
    with pytest.raises(services.ServiceError) as caught:
        services.SourceService(lambda: session, None).get(uuid4())
    assert caught.value.code == expected
    assert "secret" not in repr(caught.value) + str(caught.value) + repr(caught.value.args)
    assert session.calls == ["begin", "rollback", "close"]


@pytest.mark.parametrize("constraint,schema,table,expected", [
    ("ck_data_sources_name_key_bytes", "public", "data_sources", "validation_error"),
    ("ck_data_sources_name_edges", None, None, "validation_error"),
    ("ck_data_sources_host", "public", "data_sources", "internal_error"),
    ("ck_data_sources_name_key_bytes", "other", "data_sources", "internal_error"),
])
def test_name_check_classification(constraint, schema, table, expected):
    class DriverError(Exception):
        sqlstate = "23514"
        diag = SimpleNamespace(constraint_name=constraint, schema_name=schema, table_name=table)
    class FailingSession(Session):
        def get(self, *_): raise IntegrityError("synthetic-sql", {}, DriverError())
    with pytest.raises(services.ServiceError) as caught:
        services.SourceService(FailingSession, None).get(uuid4())
    assert caught.value.code == expected
    assert caught.value.fields == (("name",) if expected == "validation_error" else ())
