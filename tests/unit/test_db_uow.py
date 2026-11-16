"""Offline checks for the UoW contract, without a runtime UoW."""

from dataclasses import FrozenInstanceError, replace
import importlib.util
import logging
import os
import socket
import sys
from uuid import UUID, uuid4

import pytest

from dataflow_manager.db import uow


def outcome(**changes):
    values = dict(commit_status=uow.CommitStatus.NOT_ATTEMPTED,
                  rollback_status=uow.CleanupStatus.NOT_ATTEMPTED,
                  close_status=uow.CleanupStatus.NOT_ATTEMPTED,
                  primary_error_code=None, additional_errors=(), operation_id=uuid4())
    values.update(changes)
    return uow.Outcome(**values)


def test_closed_enums_and_phase_mapping():
    assert {x.value for x in uow.LifecycleState} == {"NEW", "ACTIVE", "FAILED", "COMMITTED", "ROLLED_BACK", "CLOSED"}
    assert {x.value for x in uow.CommitStatus} == {"NOT_ATTEMPTED", "SUCCEEDED", "UNKNOWN"}
    assert {x.value for x in uow.CleanupStatus} == {"NOT_ATTEMPTED", "SUCCEEDED", "FAILED", "UNKNOWN"}
    expected = {"OPEN_FAILED": "OPEN", "UOW_BEGIN_FAILED": "BEGIN",
                "UOW_EXECUTION_FAILED": "EXECUTION", "UOW_BUSINESS_ERROR": "EXECUTION",
                "UOW_COMMIT_FAILED": "COMMIT", "UOW_ROLLBACK_FAILED": "ROLLBACK",
                "UOW_CLOSE_FAILED": "CLOSE", "UOW_INVALID_STATE": "LIFECYCLE"}
    assert {code.value: uow.phase_for(code).value for code in uow.ErrorCode} == expected
    assert {p.value for p in uow.Phase} == {"OPEN", "BEGIN", "EXECUTION", "COMMIT", "ROLLBACK", "CLOSE", "LIFECYCLE"}
    assert uow.phase_for(uow.SpecialErrorCode.UOW_COMMITTED_CLEANUP_FAILED) is uow.Phase.CLOSE
    assert "UOW_COMMITTED_CLEANUP_FAILED" not in {x.value for x in uow.ErrorCode}
    for bad in ("OPEN_FAILED", "SECRET", Exception("SECRET")):
        with pytest.raises(ValueError, match="invalid UoW code"):
            uow.phase_for(bad)


def test_outcome_is_frozen_and_accepts_ordered_cleanup():
    result = outcome(commit_status=uow.CommitStatus.UNKNOWN,
                     rollback_status=uow.CleanupStatus.FAILED,
                     close_status=uow.CleanupStatus.FAILED,
                     primary_error_code=uow.ErrorCode.UOW_COMMIT_FAILED,
                     additional_errors=(uow.ErrorCode.UOW_ROLLBACK_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED))
    with pytest.raises(FrozenInstanceError):
        result.commit_status = uow.CommitStatus.SUCCEEDED
    assert isinstance(result.additional_errors, tuple)
    assert outcome().primary_error_code is None


def test_unknown_cleanup_without_failure_codes_is_allowed():
    result = outcome(commit_status=uow.CommitStatus.UNKNOWN,
                     rollback_status=uow.CleanupStatus.UNKNOWN,
                     close_status=uow.CleanupStatus.UNKNOWN)
    assert result.primary_error_code is None and result.additional_errors == ()


@pytest.mark.parametrize("field,code", [
    ("rollback_status", uow.ErrorCode.UOW_ROLLBACK_FAILED),
    ("close_status", uow.ErrorCode.UOW_CLOSE_FAILED),
])
@pytest.mark.parametrize("status", [uow.CleanupStatus.SUCCEEDED, uow.CleanupStatus.UNKNOWN,
                                     uow.CleanupStatus.NOT_ATTEMPTED])
@pytest.mark.parametrize("secondary", [False, True])
def test_failure_code_requires_failed_cleanup(field, code, status, secondary):
    primary = uow.ErrorCode.UOW_EXECUTION_FAILED if secondary else code
    additional = (code,) if secondary else ()
    values = {field: status, "primary_error_code": primary, "additional_errors": additional}
    with pytest.raises(ValueError, match="invalid UoW outcome"):
        outcome(**values)
    values[field] = uow.CleanupStatus.FAILED
    assert outcome(**values).primary_error_code is primary


@pytest.mark.parametrize("field", ["rollback_status", "close_status"])
def test_failed_cleanup_requires_failure_code(field):
    with pytest.raises(ValueError, match="invalid UoW outcome"):
        outcome(**{field: uow.CleanupStatus.FAILED})


@pytest.mark.parametrize("rollback", [uow.CleanupStatus.SUCCEEDED, uow.CleanupStatus.FAILED,
                                      uow.CleanupStatus.UNKNOWN])
def test_successful_commit_forbids_rollback(rollback):
    errors = (uow.ErrorCode.UOW_ROLLBACK_FAILED if rollback is uow.CleanupStatus.FAILED else None)
    with pytest.raises(ValueError, match="invalid UoW outcome"):
        outcome(commit_status=uow.CommitStatus.SUCCEEDED, rollback_status=rollback,
                primary_error_code=errors)
    assert outcome(commit_status=uow.CommitStatus.SUCCEEDED).commit_status is uow.CommitStatus.SUCCEEDED


@pytest.mark.parametrize("changes", [
    {"primary_error_code": uow.ErrorCode.UOW_COMMIT_FAILED},
    {"primary_error_code": uow.ErrorCode.UOW_COMMIT_FAILED, "commit_status": uow.CommitStatus.SUCCEEDED},
    {"primary_error_code": uow.ErrorCode.OPEN_FAILED, "close_status": uow.CleanupStatus.SUCCEEDED},
    {"primary_error_code": uow.ErrorCode.UOW_BEGIN_FAILED, "commit_status": uow.CommitStatus.UNKNOWN},
])
def test_open_begin_and_commit_failure_consistency(changes):
    with pytest.raises(ValueError, match="invalid UoW outcome"):
        outcome(**changes)
    assert outcome(primary_error_code=uow.ErrorCode.OPEN_FAILED).primary_error_code is uow.ErrorCode.OPEN_FAILED
    assert outcome(primary_error_code=uow.ErrorCode.UOW_BEGIN_FAILED).primary_error_code is uow.ErrorCode.UOW_BEGIN_FAILED
    assert outcome(primary_error_code=uow.ErrorCode.UOW_COMMIT_FAILED,
                   commit_status=uow.CommitStatus.UNKNOWN).commit_status is uow.CommitStatus.UNKNOWN


@pytest.mark.parametrize("changes", [
    {"commit_status": "SUCCEEDED"}, {"commit_status": uow.CleanupStatus.FAILED},
    {"rollback_status": "UNKNOWN"}, {"close_status": uow.CommitStatus.UNKNOWN},
    {"primary_error_code": "OPEN_FAILED"}, {"primary_error_code": Exception("SECRET")},
    {"primary_error_code": uow.SpecialErrorCode.UOW_COMMITTED_CLEANUP_FAILED},
    {"additional_errors": []}, {"additional_errors": (Exception("SECRET"),)},
    {"additional_errors": (uow.ErrorCode.OPEN_FAILED,)},
    {"additional_errors": (uow.ErrorCode.UOW_CLOSE_FAILED,)},
    {"primary_error_code": uow.ErrorCode.UOW_CLOSE_FAILED, "additional_errors": (uow.ErrorCode.UOW_ROLLBACK_FAILED,)},
    {"additional_errors": (uow.ErrorCode.UOW_CLOSE_FAILED, uow.ErrorCode.UOW_ROLLBACK_FAILED)},
    {"additional_errors": (uow.ErrorCode.UOW_CLOSE_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED)},
    {"primary_error_code": uow.ErrorCode.UOW_CLOSE_FAILED, "additional_errors": (uow.ErrorCode.UOW_CLOSE_FAILED,)},
    {"operation_id": "SECRET"}, {"operation_id": UUID(int=0)},
])
def test_invalid_outcome_values_are_rejected_without_echo(changes):
    with pytest.raises(ValueError) as caught:
        outcome(**changes)
    assert "SECRET" not in str(caught.value)


def test_safe_exception_and_preliminary_object():
    before = uow.UowError(uow.ErrorCode.UOW_COMMIT_FAILED)
    final_outcome = outcome(commit_status=uow.CommitStatus.UNKNOWN,
                            primary_error_code=uow.ErrorCode.UOW_COMMIT_FAILED)
    after = uow.UowError(uow.ErrorCode.UOW_COMMIT_FAILED, final_outcome)
    assert before.outcome is None and after.outcome is final_outcome
    assert str(after) == "UOW_COMMIT_FAILED"
    assert repr(after) == "UowError('UOW_COMMIT_FAILED')"
    assert after.args == ("UOW_COMMIT_FAILED",) and after.phase is uow.Phase.COMMIT
    for code, value in [("SECRET", None), (uow.ErrorCode.OPEN_FAILED, Exception("SECRET"))]:
        with pytest.raises(ValueError) as caught:
            uow.UowError(code, value)
        assert "SECRET" not in str(caught.value)


def test_special_exception_only_for_canonical_case():
    final = outcome(commit_status=uow.CommitStatus.SUCCEEDED,
                    rollback_status=uow.CleanupStatus.NOT_ATTEMPTED,
                    close_status=uow.CleanupStatus.FAILED,
                    primary_error_code=uow.ErrorCode.UOW_CLOSE_FAILED)
    special = uow.SpecialErrorCode.UOW_COMMITTED_CLEANUP_FAILED
    assert uow.UowError(special, final).phase is uow.Phase.CLOSE
    for bad in (None, replace(final, commit_status=uow.CommitStatus.UNKNOWN),
                replace(final, primary_error_code=uow.ErrorCode.UOW_EXECUTION_FAILED,
                        additional_errors=(uow.ErrorCode.UOW_CLOSE_FAILED,))):
        with pytest.raises(ValueError):
            uow.UowError(special, bad)
    assert final.commit_status is uow.CommitStatus.SUCCEEDED


def test_state_edges_and_first_error_are_pure():
    state = uow.LifecycleState
    assert uow.allows_transition(state.NEW, state.ACTIVE)
    assert uow.allows_transition(state.ACTIVE, state.FAILED)
    assert uow.allows_transition(state.ACTIVE, state.COMMITTED)
    assert uow.allows_transition(state.FAILED, state.ROLLED_BACK)
    assert uow.allows_transition(state.ROLLED_BACK, state.CLOSED)
    assert uow.allows_transition(state.COMMITTED, state.CLOSED)
    assert not uow.allows_transition(state.FAILED, state.ACTIVE)
    assert not any(uow.allows_transition(state.CLOSED, target) for target in state)
    assert uow.first_error(None, uow.ErrorCode.UOW_BEGIN_FAILED) is uow.ErrorCode.UOW_BEGIN_FAILED
    assert uow.first_error(uow.ErrorCode.UOW_BEGIN_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED) is uow.ErrorCode.UOW_BEGIN_FAILED
    with pytest.raises(ValueError):
        uow.allows_transition("NEW", state.ACTIVE)


def test_import_and_model_have_no_db_side_effects(monkeypatch):
    import sqlalchemy
    import sqlalchemy.orm

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected side effect")

    original_getitem = type(os.environ).__getitem__

    def checked_getitem(environment, key):
        if key.startswith("DFM_DB_"):
            forbidden()
        return original_getitem(environment, key)

    original_types = (uow.LifecycleState, uow.ErrorCode, uow.Outcome, uow.UowError)
    original_module = sys.modules[uow.__name__]
    original_getlogger = logging.getLogger
    original_session = sqlalchemy.orm.Session
    original_engine = sqlalchemy.create_engine
    alias = "_dataflow_uow_isolated_test"
    spec = importlib.util.spec_from_file_location(alias, uow.__file__)
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as guard:
        guard.setitem(sys.modules, alias, isolated)
        guard.setattr(sqlalchemy, "create_engine", forbidden)
        guard.setattr(sqlalchemy.orm, "Session", forbidden)
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(type(os.environ), "__getitem__", checked_getitem)
        guard.setattr(logging, "getLogger", forbidden)
        spec.loader.exec_module(isolated)
        item = outcome()
        assert uow.UowError(uow.ErrorCode.OPEN_FAILED, item).outcome is item
        assert isolated.ErrorCode is not uow.ErrorCode
        with pytest.raises(AssertionError, match="unexpected side effect"):
            sqlalchemy.create_engine("synthetic")
        with pytest.raises(AssertionError, match="unexpected side effect"):
            sqlalchemy.orm.Session()
        with pytest.raises(AssertionError, match="unexpected side effect"):
            socket.socket.connect(None, ("synthetic", 0))
        with pytest.raises(AssertionError, match="unexpected side effect"):
            logging.getLogger("synthetic")
        with pytest.raises(AssertionError, match="unexpected side effect"):
            os.environ["DFM_DB_TEST_SYNTHETIC"]
    assert alias not in sys.modules
    assert sys.modules[uow.__name__] is original_module
    assert (uow.LifecycleState, uow.ErrorCode, uow.Outcome, uow.UowError) == original_types
    assert logging.getLogger is original_getlogger
    assert sqlalchemy.orm.Session is original_session and sqlalchemy.create_engine is original_engine
    assert type(os.environ).__getitem__ is original_getitem
    assert uow.UnitOfWork(lambda: forbidden()).state is uow.LifecycleState.NEW


class FakeSession:
    def __init__(self, faults=None):
        self.calls = []
        self.faults = faults or {}

    def _call(self, phase):
        self.calls.append(phase)
        if phase in self.faults:
            raise self.faults[phase]

    def begin(self): self._call("begin")
    def commit(self): self._call("commit")
    def rollback(self): self._call("rollback")
    def close(self): self._call("close")


def test_runtime_no_autocommit_and_session_ownership():
    session = FakeSession()
    factories = []
    work = uow.UnitOfWork(lambda: factories.append(1) or session)
    assert factories == [] and work.outcome is None
    with work as active:
        assert active is work and work.state is uow.LifecycleState.ACTIVE
    assert factories == [1] and session.calls == ["begin", "rollback", "close"]
    assert work.outcome.rollback_status is uow.CleanupStatus.SUCCEEDED
    assert work.outcome.commit_status is uow.CommitStatus.NOT_ATTEMPTED
    assert not hasattr(work, "session") and not hasattr(work, "execute")
    final = work.outcome
    for action in (work.commit, lambda: work.__enter__(), lambda: work.mark_failed(uow.ErrorCode.UOW_BUSINESS_ERROR)):
        with pytest.raises(uow.UowError) as caught:
            action()
        assert caught.value.code is uow.ErrorCode.UOW_INVALID_STATE and caught.value.outcome is final


def test_runtime_commit_and_repeat_commit_after_success():
    session = FakeSession()
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work:
            work.commit()
            with pytest.raises(uow.UowError):
                work.commit()
    assert caught.value.code is uow.ErrorCode.UOW_INVALID_STATE
    assert work.outcome.commit_status is uow.CommitStatus.SUCCEEDED
    assert session.calls == ["begin", "commit", "close"]


@pytest.mark.parametrize("phase,code,calls", [
    ("begin", uow.ErrorCode.UOW_BEGIN_FAILED, ["begin", "rollback", "close"]),
    ("commit", uow.ErrorCode.UOW_COMMIT_FAILED, ["begin", "commit", "rollback", "close"]),
    ("rollback", uow.ErrorCode.UOW_ROLLBACK_FAILED, ["begin", "rollback", "close"]),
    ("close", uow.ErrorCode.UOW_CLOSE_FAILED, ["begin", "rollback", "close"]),
])
def test_runtime_individual_failures_are_safe(phase, code, calls):
    secret = "synthetic-raw-secret-dsn-SQL"
    session = FakeSession({phase: RuntimeError(secret)})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work:
            if phase == "commit":
                work.commit()
    assert session.calls == calls and caught.value.code is code
    assert secret not in str(caught.value) + repr(caught.value) + repr(work.outcome)
    assert work.outcome.primary_error_code is code


def test_factory_failure_and_begin_cleanup_failure():
    def fail(): raise RuntimeError("synthetic-secret")
    work = uow.UnitOfWork(fail)
    with pytest.raises(uow.UowError) as caught:
        with work: pass
    assert caught.value.code is uow.ErrorCode.OPEN_FAILED
    assert work.outcome.close_status is uow.CleanupStatus.NOT_ATTEMPTED
    session = FakeSession({"begin": RuntimeError("secret"), "close": RuntimeError("secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work: pass
    assert caught.value.code is uow.ErrorCode.UOW_BEGIN_FAILED
    assert work.outcome.additional_errors == (uow.ErrorCode.UOW_CLOSE_FAILED,)


def test_body_and_both_cleanup_errors_preserve_priority():
    session = FakeSession({"rollback": RuntimeError("rollback-secret"), "close": RuntimeError("close-secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work: raise RuntimeError("body-secret")
    assert caught.value.code is uow.ErrorCode.UOW_EXECUTION_FAILED
    assert work.outcome.additional_errors == (uow.ErrorCode.UOW_ROLLBACK_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED)
    assert "secret" not in repr(caught.value) + repr(work.outcome)


def test_commit_and_both_cleanup_errors_preserve_priority():
    session = FakeSession({phase: RuntimeError("synthetic-secret")
                           for phase in ("commit", "rollback", "close")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work: work.commit()
    assert caught.value.code is uow.ErrorCode.UOW_COMMIT_FAILED
    assert work.outcome.commit_status is uow.CommitStatus.UNKNOWN
    assert work.outcome.additional_errors == (uow.ErrorCode.UOW_ROLLBACK_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED)
    assert session.calls == ["begin", "commit", "rollback", "close"]


def test_multiple_invalid_operations_do_not_replace_first_error():
    session = FakeSession({"commit": RuntimeError("secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work:
            with pytest.raises(uow.UowError): work.commit()
            for action in (work.commit, work.__enter__, lambda: work.mark_failed(uow.ErrorCode.UOW_BUSINESS_ERROR)):
                with pytest.raises(uow.UowError): action()
    assert caught.value.code is uow.ErrorCode.UOW_COMMIT_FAILED
    assert session.calls == ["begin", "commit", "rollback", "close"]


def test_business_and_caught_commit_error_remain_failures():
    session = FakeSession({"commit": RuntimeError("secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work:
            with pytest.raises(uow.UowError) as preliminary: work.commit()
            assert preliminary.value.outcome is None
            with pytest.raises(uow.UowError): work.commit()
    assert caught.value.code is uow.ErrorCode.UOW_COMMIT_FAILED
    assert caught.value.outcome.commit_status is uow.CommitStatus.UNKNOWN
    assert session.calls == ["begin", "commit", "rollback", "close"]
    work = uow.UnitOfWork(FakeSession)
    with pytest.raises(uow.UowError) as caught:
        with work:
            work.mark_failed(uow.ErrorCode.UOW_BUSINESS_ERROR)
            with pytest.raises(uow.UowError): work.mark_failed(uow.ErrorCode.UOW_BUSINESS_ERROR)
    assert caught.value.code is uow.ErrorCode.UOW_BUSINESS_ERROR


def test_committed_close_failure_and_body_after_commit():
    session = FakeSession({"close": RuntimeError("close-secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work: work.commit()
    assert caught.value.code is uow.SpecialErrorCode.UOW_COMMITTED_CLEANUP_FAILED
    assert work.outcome.commit_status is uow.CommitStatus.SUCCEEDED
    assert session.calls == ["begin", "commit", "close"]
    session = FakeSession({"close": RuntimeError("close-secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError) as caught:
        with work:
            work.commit()
            raise ValueError("body-secret")
    assert caught.value.code is uow.ErrorCode.UOW_EXECUTION_FAILED
    assert work.outcome.additional_errors == (uow.ErrorCode.UOW_CLOSE_FAILED,)
    assert work.outcome.commit_status is uow.CommitStatus.SUCCEEDED


@pytest.mark.parametrize("phase", ["begin", "commit", "rollback", "close"])
@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_base_exception_per_phase_preserves_first_interrupt(phase, exception_type):
    interrupt = exception_type("synthetic-secret")
    session = FakeSession({phase: interrupt})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(exception_type) as caught:
        with work:
            if phase == "commit": work.commit()
    assert caught.value is interrupt and work._interrupt is None
    assert session.calls[-1] == "close"
    status = {"commit": work.outcome.commit_status, "rollback": work.outcome.rollback_status,
              "close": work.outcome.close_status}
    if phase != "begin": assert status[phase].value == "UNKNOWN"


def test_q15_caught_interrupt_then_cleanup_and_priority():
    interrupt = KeyboardInterrupt("secret")
    session = FakeSession({"commit": interrupt, "rollback": RuntimeError("rollback-secret"),
                           "close": RuntimeError("close-secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(KeyboardInterrupt) as caught:
        with work:
            with pytest.raises(KeyboardInterrupt): work.commit()
            with pytest.raises(uow.UowError): work.commit()
    assert caught.value is interrupt and work._interrupt is None
    assert session.calls == ["begin", "commit", "rollback", "close"]
    assert work.outcome.commit_status is uow.CommitStatus.UNKNOWN
    assert work.outcome.primary_error_code is uow.ErrorCode.UOW_INVALID_STATE
    assert work.outcome.additional_errors == (uow.ErrorCode.UOW_ROLLBACK_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED)


def test_invalid_enter_before_and_during_context():
    session = FakeSession()
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(uow.UowError): work.commit()
    assert work.state is uow.LifecycleState.NEW
    with pytest.raises(uow.UowError) as caught:
        with work:
            with pytest.raises(uow.UowError): work.__enter__()
    assert caught.value.code is uow.ErrorCode.UOW_INVALID_STATE
    assert session.calls == ["begin", "rollback", "close"]


def test_factory_interrupt_and_cleanup_interrupt_priority():
    first = KeyboardInterrupt("first-secret")
    def fail(): raise first
    work = uow.UnitOfWork(fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        with work: pass
    assert caught.value is first and work.outcome.close_status is uow.CleanupStatus.NOT_ATTEMPTED
    second = SystemExit("second-secret")
    session = FakeSession({"rollback": first, "close": second})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(KeyboardInterrupt) as caught:
        with work: raise RuntimeError("body-secret")
    assert caught.value is first
    assert work.outcome.primary_error_code is uow.ErrorCode.UOW_EXECUTION_FAILED
    assert work.outcome.rollback_status is uow.CleanupStatus.UNKNOWN
    assert work.outcome.close_status is uow.CleanupStatus.UNKNOWN


def test_q15_caught_interrupt_then_body_error():
    first = KeyboardInterrupt("interrupt-secret")
    session = FakeSession({"commit": first})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(KeyboardInterrupt) as caught:
        with work:
            with pytest.raises(KeyboardInterrupt): work.commit()
            raise ValueError("body-secret")
    assert caught.value is first and work._interrupt is None
    assert work.outcome.primary_error_code is uow.ErrorCode.UOW_EXECUTION_FAILED
    assert work.outcome.commit_status is uow.CommitStatus.UNKNOWN


def test_q15_caught_interrupt_then_second_interrupt():
    first = KeyboardInterrupt("first-secret")
    session = FakeSession({"commit": first, "rollback": SystemExit("second-secret")})
    work = uow.UnitOfWork(lambda: session)
    with pytest.raises(KeyboardInterrupt) as caught:
        with work:
            with pytest.raises(KeyboardInterrupt): work.commit()
            raise GeneratorExit("body-secret")
    assert caught.value is first
    assert work.outcome.commit_status is uow.CommitStatus.UNKNOWN
    assert work.outcome.rollback_status is uow.CleanupStatus.UNKNOWN
    assert work.outcome.close_status is uow.CleanupStatus.SUCCEEDED
    assert work.outcome.primary_error_code is None


def test_q15_caught_interrupt_then_mark_failed_is_invalid():
    first = KeyboardInterrupt("secret")
    work = uow.UnitOfWork(lambda: FakeSession({"commit": first}))
    with pytest.raises(KeyboardInterrupt):
        with work:
            with pytest.raises(KeyboardInterrupt): work.commit()
            with pytest.raises(uow.UowError): work.mark_failed(uow.ErrorCode.UOW_BUSINESS_ERROR)
    assert work.outcome.primary_error_code is uow.ErrorCode.UOW_INVALID_STATE


def test_positive_log_control_and_safe_runtime(caplog):
    secret = "synthetic-unique-password-dsn-SQL-bind-secret"
    logger = logging.getLogger("test.uow.positive_control")
    with caplog.at_level(logging.ERROR):
        logger.error("controlled leak: %s", secret)
        assert secret in caplog.text
        caplog.clear()
        session = FakeSession({"rollback": RuntimeError(secret), "close": RuntimeError(secret)})
        work = uow.UnitOfWork(lambda: session)
        with pytest.raises(uow.UowError) as caught:
            with work: raise RuntimeError(secret)
        assert secret not in caplog.text
        assert "UOW_ROLLBACK_FAILED" in caplog.text and "UOW_CLOSE_FAILED" in caplog.text
        assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
        assert secret not in repr(work.outcome) + str(caught.value) + repr(caught.value)


def test_diagnostic_failure_does_not_replace_primary(monkeypatch):
    def fail_logger(*args, **kwargs): raise RuntimeError("logger-secret")
    session = FakeSession({"rollback": RuntimeError("rollback-secret"), "close": RuntimeError("close-secret")})
    work = uow.UnitOfWork(lambda: session)
    with monkeypatch.context() as guard:
        guard.setattr(logging, "getLogger", fail_logger)
        with pytest.raises(uow.UowError) as caught:
            with work: raise RuntimeError("body-secret")
    assert caught.value.code is uow.ErrorCode.UOW_EXECUTION_FAILED
    assert work.outcome.additional_errors == (uow.ErrorCode.UOW_ROLLBACK_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED)
    assert session.calls == ["begin", "rollback", "close"]


@pytest.mark.parametrize("diagnostic_interrupt", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("prior_commit_interrupt", [False, True])
def test_diagnostic_interrupt_preserves_priority_and_finishes_cleanup(monkeypatch, diagnostic_interrupt, prior_commit_interrupt):
    first = KeyboardInterrupt("first-sensitive-interrupt")
    second = diagnostic_interrupt("diagnostic-sensitive-interrupt")
    session = FakeSession({"rollback": RuntimeError("rollback-sensitive"),
                           "close": RuntimeError("close-sensitive"),
                           **({"commit": first} if prior_commit_interrupt else {})})
    work = uow.UnitOfWork(lambda: session)

    class FailingLogger:
        def error(self, *args, **kwargs):
            raise second

    original_get_logger = logging.getLogger
    def get_logger(name=None):
        return FailingLogger() if name == "dataflow_manager.db.uow" else original_get_logger(name)

    with monkeypatch.context() as guard:
        guard.setattr(logging, "getLogger", get_logger)
        with pytest.raises(KeyboardInterrupt if prior_commit_interrupt else diagnostic_interrupt) as caught:
            with work:
                if prior_commit_interrupt:
                    with pytest.raises(KeyboardInterrupt): work.commit()
                else:
                    raise RuntimeError("body-sensitive")
    assert caught.value is (first if prior_commit_interrupt else second)
    assert session.calls == (["begin", "commit", "rollback", "close"] if prior_commit_interrupt
                             else ["begin", "rollback", "close"])
    assert work.state is uow.LifecycleState.CLOSED and work._interrupt is None
    assert work.outcome.rollback_status is uow.CleanupStatus.FAILED
    assert work.outcome.close_status is uow.CleanupStatus.FAILED
    if prior_commit_interrupt:
        assert work.outcome.commit_status is uow.CommitStatus.UNKNOWN
        assert work.outcome.primary_error_code is uow.ErrorCode.UOW_ROLLBACK_FAILED
        assert work.outcome.additional_errors == (uow.ErrorCode.UOW_CLOSE_FAILED,)
    else:
        assert work.outcome.primary_error_code is uow.ErrorCode.UOW_EXECUTION_FAILED
        assert work.outcome.additional_errors == (uow.ErrorCode.UOW_ROLLBACK_FAILED, uow.ErrorCode.UOW_CLOSE_FAILED)


def test_cleanup_records_are_fixed_for_root_and_app_handlers():
    secret = "unique-synthetic-password-dsn-SQL-bind-for-records"

    class Collector(logging.Handler):
        def __init__(self):
            super().__init__(logging.ERROR)
            self.records = []

        def emit(self, record): self.records.append(record)

    root, app = logging.getLogger(), logging.getLogger("dataflow_manager.db.uow")
    root_handler, app_handler = Collector(), Collector()
    root.addHandler(root_handler)
    app.addHandler(app_handler)
    try:
        logging.getLogger("positive.uow").error("control %s", secret)
        assert any(secret in record.getMessage() for record in root_handler.records)
        root_handler.records.clear()
        session = FakeSession({"rollback": RuntimeError(secret), "close": RuntimeError(secret)})
        with pytest.raises(uow.UowError):
            with uow.UnitOfWork(lambda: session): raise ValueError(secret)
        for handler in (root_handler, app_handler):
            assert [record.msg for record in handler.records] == ["UOW_ROLLBACK_FAILED", "UOW_CLOSE_FAILED"]
            for record in handler.records:
                assert record.args == () and record.exc_info is None and record.stack_info is None
                assert secret not in str(record.__dict__)
    finally:
        app.removeHandler(app_handler)
        root.removeHandler(root_handler)


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_committed_close_failure_with_diagnostic_interrupt(monkeypatch, caplog, kind):
    secret = "synthetic-close-and-diagnostic-secret"
    interrupt = kind(secret)
    session = FakeSession({"close": RuntimeError(secret)})
    work = uow.UnitOfWork(lambda: session)
    original = logging.getLogger

    class InterruptedLogger:
        def error(self, *args, **kwargs):
            raise interrupt

    def get_logger(name=None):
        return InterruptedLogger() if name == "dataflow_manager.db.uow" else original(name)

    with monkeypatch.context() as guard:
        guard.setattr(logging, "getLogger", get_logger)
        with pytest.raises(kind) as caught:
            with work:
                work.commit()
    assert caught.value is interrupt and work._interrupt is None
    assert session.calls == ["begin", "commit", "close"]
    assert work.state is uow.LifecycleState.CLOSED
    assert work.outcome.commit_status is uow.CommitStatus.SUCCEEDED
    assert work.outcome.rollback_status is uow.CleanupStatus.NOT_ATTEMPTED
    assert work.outcome.close_status is uow.CleanupStatus.FAILED
    assert work.outcome.primary_error_code is uow.ErrorCode.UOW_CLOSE_FAILED
    assert work.outcome.additional_errors == () and secret not in caplog.text
    with pytest.raises(FrozenInstanceError):
        work.outcome.close_status = uow.CleanupStatus.SUCCEEDED
