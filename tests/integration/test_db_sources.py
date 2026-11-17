"""Opt-in live repository checks against the approved isolated database.

Only UUID-tagged synthetic records are changed. Committed and deleted IDs are
recorded before and after commit in the run ledger; no cleanup is automatic.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from uuid import uuid4

import pytest
from sqlalchemy import event

from dataflow_manager.db.sources import SourceNameConflict, SourceRepository
from dataflow_manager.db.uow import ErrorCode, LifecycleState, UnitOfWork, UowError

_spec = importlib.util.spec_from_file_location("_repository_test_gate", Path(__file__).with_name("conftest.py"))
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def _input(name):
    return dict(name=name, host="example.test", port=5432, database_name="source_db",
                username="source_user", password_encrypted="synthetic-encrypted-token")


def _append_ledger(database, source_id, status):
    # Additional deletion events use the same path validation and append-only
    # discipline as the approved TASK-005 probe ledger.
    path = _gate.ledger_path(os.environ)
    if database != os.environ.get("DFM_TEST_APPROVED_DATABASE") or source_id.version != 4:
        raise RuntimeError("REPOSITORY_LEDGER_REJECTED")
    if status not in ("delete_intended", "deleted"):
        raise RuntimeError("REPOSITORY_LEDGER_REJECTED")
    payload = (json.dumps({"database": database, "probe_id": source_id.hex,
                           "status": status, "kind": "data_sources"}) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise RuntimeError("REPOSITORY_LEDGER_WRITE_FAILED")
    finally:
        os.close(fd)


def _create_committed(config, maker, name):
    with UnitOfWork(maker) as work:
        source = SourceRepository(work).create(**_input(name))
        source_id = source.id
        _gate.record_probe(os.environ, config.name, source_id.hex, "intended")
        work.commit()
        _gate.record_probe(os.environ, config.name, source_id.hex, "committed")
    return source_id


def test_crud_noop_token_and_physical_delete(task005_real_integration):
    config, engine, maker = task005_real_integration
    source_id = _create_committed(config, maker, "repo_" + uuid4().hex)
    missing = uuid4()
    with UnitOfWork(maker) as work:
        repo = SourceRepository(work)
        source = repo.get(source_id)
        assert source.id == source_id and source.password_encrypted == "synthetic-encrypted-token"
        assert any(item.id == source_id for item in repo.list())
        assert repo.get(missing) is None
        original_timestamp = source.updated_at
        calls = []
        def observe(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("UPDATE"):
                calls.append(True)
        event.listen(engine, "before_cursor_execute", observe)
        try:
            assert repo.update(source_id, {"host": "example.test"}).updated_at == original_timestamp
            assert calls == []
            assert repo.update(source_id, {"password_encrypted": "synthetic-encrypted-token"}) is source
            assert len(calls) == 1
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert repo.update(source_id, {"host": "new.example.test"}).password_encrypted == "synthetic-encrypted-token"
        assert repo.update(source_id, {"password_encrypted": "new-synthetic-token"}).password_encrypted == "new-synthetic-token"
        assert repo.update(missing, {"host": "new.example.test"}) is None
        work.commit()
    with UnitOfWork(maker) as work:
        repo = SourceRepository(work)
        assert repo.get(source_id).password_encrypted == "new-synthetic-token"
        assert repo.delete(missing) is False
        assert repo.delete(source_id) is True
        assert repo.get(source_id) is None
        _append_ledger(config.name, source_id, "delete_intended")
        work.commit()
        _append_ledger(config.name, source_id, "deleted")
    with UnitOfWork(maker) as work:
        assert SourceRepository(work).get(source_id) is None


def test_integrity_failure_rolls_back_and_guards_caught_failure(task005_real_integration):
    config, _, maker = task005_real_integration
    with pytest.raises(UowError) as final:
        with UnitOfWork(maker) as work:
            repo = SourceRepository(work)
            source = repo.create(**_input("rollback_" + uuid4().hex))
            source_id = source.id
            _gate.record_probe(os.environ, config.name, source_id.hex, "intended")
            with pytest.raises(UowError) as caught:
                repo.update(source_id, {"port": 0})
            assert not isinstance(caught.value, SourceNameConflict)
            assert caught.value.code is ErrorCode.UOW_EXECUTION_FAILED
            assert work.state is LifecycleState.FAILED
            with pytest.raises(UowError):
                repo.get(source_id)
            with pytest.raises(UowError):
                work.commit()
    assert final.value.outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED
    with UnitOfWork(maker) as work:
        assert SourceRepository(work).get(source_id) is None


def _race(maker, config, action):
    barrier = Barrier(2, timeout=10)
    def worker(index):
        classified_conflict = False
        try:
            with UnitOfWork(maker) as work:
                repo = SourceRepository(work)
                barrier.wait()
                try:
                    source = action(repo, index)
                except SourceNameConflict:
                    assert work.state is LifecycleState.FAILED
                    with pytest.raises(UowError):
                        work.commit()
                    classified_conflict = True
                    return "conflict"
                source_id = source.id
                _gate.record_probe(os.environ, config.name, source_id.hex, "intended")
                work.commit()
                _gate.record_probe(os.environ, config.name, source_id.hex, "committed")
                return "committed"
        except UowError as error:
            if (classified_conflict and error.outcome is not None
                    and error.outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED):
                return "conflict"
            raise
        except BrokenBarrierError:
            pytest.fail("repository race barrier failed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, i) for i in range(2)]
        return sorted(future.result(timeout=30) for future in futures)


def test_concurrent_casefold_create_has_one_conflict(task005_real_integration):
    config, _, maker = task005_real_integration
    name = "racecreate_" + uuid4().hex
    outcomes = _race(maker, config, lambda repo, i: repo.create(**_input(name if i == 0 else name.upper())))
    assert outcomes == ["committed", "conflict"]
    with UnitOfWork(maker) as work:
        matches = [x for x in SourceRepository(work).list() if x.name.casefold() == name.casefold()]
        assert len(matches) == 1


def test_concurrent_rename_has_one_conflict_and_atomic_rollback(task005_real_integration):
    config, _, maker = task005_real_integration
    ids = [_create_committed(config, maker, "renamesource_" + uuid4().hex) for _ in range(2)]
    target = "racetarget_" + uuid4().hex
    outcomes = _race(maker, config, lambda repo, i: repo.update(ids[i], {"name": target if i == 0 else target.upper()}))
    assert outcomes == ["committed", "conflict"]
    with UnitOfWork(maker) as work:
        repo = SourceRepository(work)
        names = [repo.get(source_id).name for source_id in ids]
        assert sum(name.casefold() == target.casefold() for name in names) == 1
        assert sum(name.startswith("renamesource_") for name in names) == 1


def test_concurrent_delete_serializes_and_reports_missing(task005_real_integration):
    config, _, maker = task005_real_integration
    source_id = _create_committed(config, maker, "deletesource_" + uuid4().hex)
    barrier = Barrier(2, timeout=10)
    def worker():
        with UnitOfWork(maker) as work:
            repo = SourceRepository(work)
            barrier.wait()
            deleted = repo.delete(source_id)
            if deleted:
                _append_ledger(config.name, source_id, "delete_intended")
            work.commit()
            if deleted:
                _append_ledger(config.name, source_id, "deleted")
            return deleted
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        assert sorted(future.result(timeout=30) for future in futures) == [False, True]
    with UnitOfWork(maker) as work:
        assert SourceRepository(work).get(source_id) is None


def test_update_refreshes_stale_identity_after_other_transaction(task005_real_integration):
    config, _, maker = task005_real_integration
    source_id = _create_committed(config, maker, "stalemapping_" + uuid4().hex)
    with UnitOfWork(maker) as first:
        repo_first = SourceRepository(first)
        held_source = repo_first.get(source_id)
        original = held_source.host
        with UnitOfWork(maker) as second:
            assert SourceRepository(second).update(source_id, {"host": "changed.example.test"}) is not None
            _gate.record_probe(os.environ, config.name, source_id.hex, "intended")
            second.commit()
            _gate.record_probe(os.environ, config.name, source_id.hex, "committed")
        # The first Session already holds the original ORM object. Its locked
        # read must refresh the row committed by the second Session before it
        # decides whether restoring the original value is a no-op.
        assert repo_first.update(source_id, {"host": original}).host == original
        _gate.record_probe(os.environ, config.name, source_id.hex, "intended")
        first.commit()
        _gate.record_probe(os.environ, config.name, source_id.hex, "committed")
    with UnitOfWork(maker) as verify:
        assert SourceRepository(verify).get(source_id).host == original
