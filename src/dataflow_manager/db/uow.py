"""UoW contract types; this module does not implement a unit of work."""

from dataclasses import dataclass
from enum import StrEnum
import logging
from types import MappingProxyType
from uuid import UUID, uuid4


class LifecycleState(StrEnum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    CLOSED = "CLOSED"


class CommitStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    SUCCEEDED = "SUCCEEDED"
    UNKNOWN = "UNKNOWN"


class CleanupStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ErrorCode(StrEnum):
    OPEN_FAILED = "OPEN_FAILED"
    UOW_BEGIN_FAILED = "UOW_BEGIN_FAILED"
    UOW_EXECUTION_FAILED = "UOW_EXECUTION_FAILED"
    UOW_BUSINESS_ERROR = "UOW_BUSINESS_ERROR"
    UOW_COMMIT_FAILED = "UOW_COMMIT_FAILED"
    UOW_ROLLBACK_FAILED = "UOW_ROLLBACK_FAILED"
    UOW_CLOSE_FAILED = "UOW_CLOSE_FAILED"
    UOW_INVALID_STATE = "UOW_INVALID_STATE"


class Phase(StrEnum):
    OPEN = "OPEN"
    BEGIN = "BEGIN"
    EXECUTION = "EXECUTION"
    COMMIT = "COMMIT"
    ROLLBACK = "ROLLBACK"
    CLOSE = "CLOSE"
    LIFECYCLE = "LIFECYCLE"


class SpecialErrorCode(StrEnum):
    UOW_COMMITTED_CLEANUP_FAILED = "UOW_COMMITTED_CLEANUP_FAILED"


_PHASES = MappingProxyType({
    ErrorCode.OPEN_FAILED: Phase.OPEN,
    ErrorCode.UOW_BEGIN_FAILED: Phase.BEGIN,
    ErrorCode.UOW_EXECUTION_FAILED: Phase.EXECUTION,
    ErrorCode.UOW_BUSINESS_ERROR: Phase.EXECUTION,
    ErrorCode.UOW_COMMIT_FAILED: Phase.COMMIT,
    ErrorCode.UOW_ROLLBACK_FAILED: Phase.ROLLBACK,
    ErrorCode.UOW_CLOSE_FAILED: Phase.CLOSE,
    ErrorCode.UOW_INVALID_STATE: Phase.LIFECYCLE,
    SpecialErrorCode.UOW_COMMITTED_CLEANUP_FAILED: Phase.CLOSE,
})


def phase_for(code: ErrorCode | SpecialErrorCode) -> Phase:
    if type(code) not in (ErrorCode, SpecialErrorCode):
        raise ValueError("invalid UoW code")
    return _PHASES[code]


@dataclass(frozen=True, slots=True)
class Outcome:
    commit_status: CommitStatus
    rollback_status: CleanupStatus
    close_status: CleanupStatus
    primary_error_code: ErrorCode | None
    additional_errors: tuple[ErrorCode, ...]
    operation_id: UUID

    def __post_init__(self) -> None:
        if (type(self.commit_status) is not CommitStatus
                or type(self.rollback_status) is not CleanupStatus
                or type(self.close_status) is not CleanupStatus
                or (self.primary_error_code is not None and type(self.primary_error_code) is not ErrorCode)
                or type(self.additional_errors) is not tuple
                or type(self.operation_id) is not UUID
                or self.operation_id.version != 4):
            raise ValueError("invalid UoW outcome")
        permitted = (ErrorCode.UOW_ROLLBACK_FAILED, ErrorCode.UOW_CLOSE_FAILED)
        errors = self.additional_errors
        if (any(type(code) is not ErrorCode or code not in permitted for code in errors)
                or (errors and self.primary_error_code is None)
                or len(errors) != len(set(errors))
                or self.primary_error_code in errors
                or (self.primary_error_code is ErrorCode.UOW_CLOSE_FAILED
                    and ErrorCode.UOW_ROLLBACK_FAILED in errors)
                or (len(errors) == 2 and errors != permitted)):
            raise ValueError("invalid UoW outcome")
        all_errors = (self.primary_error_code,) + errors
        for code, status in ((ErrorCode.UOW_ROLLBACK_FAILED, self.rollback_status),
                             (ErrorCode.UOW_CLOSE_FAILED, self.close_status)):
            if (code in all_errors) != (status is CleanupStatus.FAILED):
                raise ValueError("invalid UoW outcome")
        if ((self.commit_status is CommitStatus.SUCCEEDED
             and self.rollback_status is not CleanupStatus.NOT_ATTEMPTED)
                or (self.primary_error_code is ErrorCode.UOW_COMMIT_FAILED
                    and self.commit_status is not CommitStatus.UNKNOWN)
                or (self.primary_error_code is ErrorCode.OPEN_FAILED
                    and (self.commit_status is not CommitStatus.NOT_ATTEMPTED
                         or self.rollback_status is not CleanupStatus.NOT_ATTEMPTED
                         or self.close_status is not CleanupStatus.NOT_ATTEMPTED))
                or (self.primary_error_code is ErrorCode.UOW_BEGIN_FAILED
                    and self.commit_status is not CommitStatus.NOT_ATTEMPTED)):
            raise ValueError("invalid UoW outcome")


class UowError(RuntimeError):
    """Safe code and optional outcome; traceback/context are not safe to log."""

    def __init__(self, code: ErrorCode | SpecialErrorCode, outcome: Outcome | None = None) -> None:
        phase = phase_for(code)
        if outcome is not None and type(outcome) is not Outcome:
            raise ValueError("invalid UoW exception")
        if code is SpecialErrorCode.UOW_COMMITTED_CLEANUP_FAILED:
            if (outcome is None or outcome.commit_status is not CommitStatus.SUCCEEDED
                    or outcome.rollback_status is not CleanupStatus.NOT_ATTEMPTED
                    or outcome.close_status is not CleanupStatus.FAILED
                    or outcome.primary_error_code is not ErrorCode.UOW_CLOSE_FAILED
                    or outcome.additional_errors != ()):
                raise ValueError("invalid UoW exception")
        self.code = code
        self.phase = phase
        self.outcome = outcome
        super().__init__(code.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code.value!r})"


def first_error(current: ErrorCode | None, candidate: ErrorCode) -> ErrorCode:
    """Retain the first registered ordinary error without performing cleanup."""
    if (current is not None and type(current) is not ErrorCode) or type(candidate) is not ErrorCode:
        raise ValueError("invalid UoW code")
    return candidate if current is None else current


_TRANSITIONS = MappingProxyType({
    LifecycleState.NEW: frozenset((LifecycleState.ACTIVE, LifecycleState.FAILED, LifecycleState.CLOSED)),
    LifecycleState.ACTIVE: frozenset((LifecycleState.FAILED, LifecycleState.COMMITTED,
                                     LifecycleState.ROLLED_BACK, LifecycleState.CLOSED)),
    LifecycleState.FAILED: frozenset((LifecycleState.ROLLED_BACK, LifecycleState.CLOSED)),
    LifecycleState.COMMITTED: frozenset((LifecycleState.CLOSED,)),
    LifecycleState.ROLLED_BACK: frozenset((LifecycleState.CLOSED,)),
    LifecycleState.CLOSED: frozenset(),
})


def allows_transition(current: LifecycleState, target: LifecycleState) -> bool:
    """Check state edges only; the runtime must also validate the triggering event."""
    if type(current) is not LifecycleState or type(target) is not LifecycleState:
        raise ValueError("invalid UoW state")
    return target in _TRANSITIONS[current]


class UnitOfWork:
    """Single-use synchronous transaction boundary; session remains private."""

    def __init__(self, session_factory):
        self._factory = session_factory
        self._session = None
        self._state = LifecycleState.NEW
        self._commit = CommitStatus.NOT_ATTEMPTED
        self._rollback = CleanupStatus.NOT_ATTEMPTED
        self._close = CleanupStatus.NOT_ATTEMPTED
        self._primary = None
        self._additional = []
        self._interrupt = None
        self._outcome = None
        self._operation_id = uuid4()

    @property
    def state(self):
        return self._state

    @property
    def outcome(self):
        return self._outcome

    def _record(self, code):
        if self._primary is None:
            self._primary = code
        elif code in (ErrorCode.UOW_ROLLBACK_FAILED, ErrorCode.UOW_CLOSE_FAILED):
            self._additional.append(code)

    def _invalid(self):
        if self._state not in (LifecycleState.NEW, LifecycleState.CLOSED):
            self._record(ErrorCode.UOW_INVALID_STATE)
            if self._state is LifecycleState.ACTIVE:
                self._state = LifecycleState.FAILED
        raise UowError(ErrorCode.UOW_INVALID_STATE, self._outcome)

    def _interrupted(self, error):
        if self._interrupt is None:
            self._interrupt = error

    def _diagnose(self, code):
        try:
            logging.getLogger("dataflow_manager.db.uow").error(code.value)
        except Exception:
            # Diagnostic failures cannot replace the transaction's primary error.
            pass
        except BaseException as error:
            # Preserve the first interruption, then complete available cleanup.
            self._interrupted(error)

    def __enter__(self):
        if self._state is not LifecycleState.NEW:
            return self._invalid()
        try:
            self._session = self._factory()
        except Exception:
            self._record(ErrorCode.OPEN_FAILED)
            self._finish()
            self._raise_final()
        except BaseException as error:
            self._interrupted(error)
            self._finish()
            self._raise_final()
        try:
            self._session.begin()
        except Exception:
            self._record(ErrorCode.UOW_BEGIN_FAILED)
            self._state = LifecycleState.FAILED
            self._finish()
            self._raise_final()
        except BaseException as error:
            self._interrupted(error)
            self._state = LifecycleState.FAILED
            self._finish()
            self._raise_final()
        self._state = LifecycleState.ACTIVE
        return self

    def commit(self):
        if self._state is not LifecycleState.ACTIVE:
            return self._invalid()
        self._commit = CommitStatus.UNKNOWN
        try:
            self._session.commit()
        except Exception:
            self._record(ErrorCode.UOW_COMMIT_FAILED)
            self._state = LifecycleState.FAILED
            raise UowError(ErrorCode.UOW_COMMIT_FAILED) from None
        except BaseException as error:
            self._interrupted(error)
            self._state = LifecycleState.FAILED
            raise
        self._commit = CommitStatus.SUCCEEDED
        self._state = LifecycleState.COMMITTED

    def mark_failed(self, code):
        if self._state is not LifecycleState.ACTIVE or code is not ErrorCode.UOW_BUSINESS_ERROR:
            return self._invalid()
        self._record(code)
        self._state = LifecycleState.FAILED

    def __exit__(self, exc_type, exc, traceback):
        if self._state in (LifecycleState.NEW, LifecycleState.CLOSED):
            return self._invalid()
        if exc is not None:
            if not isinstance(exc, Exception):
                self._interrupted(exc)
            elif self._primary is None:
                self._record(ErrorCode.UOW_EXECUTION_FAILED)
        self._finish()
        self._raise_final()
        return False

    def _finish(self):
        if self._session is not None:
            if self._commit is not CommitStatus.SUCCEEDED:
                try:
                    self._session.rollback()
                except Exception:
                    self._rollback = CleanupStatus.FAILED
                    self._record(ErrorCode.UOW_ROLLBACK_FAILED)
                    self._diagnose(ErrorCode.UOW_ROLLBACK_FAILED)
                except BaseException as error:
                    self._rollback = CleanupStatus.UNKNOWN
                    self._interrupted(error)
                else:
                    self._rollback = CleanupStatus.SUCCEEDED
            try:
                self._session.close()
            except Exception:
                self._close = CleanupStatus.FAILED
                self._record(ErrorCode.UOW_CLOSE_FAILED)
                self._diagnose(ErrorCode.UOW_CLOSE_FAILED)
            except BaseException as error:
                self._close = CleanupStatus.UNKNOWN
                self._interrupted(error)
            else:
                self._close = CleanupStatus.SUCCEEDED
        self._session = None
        self._state = LifecycleState.CLOSED
        self._outcome = Outcome(self._commit, self._rollback, self._close,
                                self._primary, tuple(self._additional), self._operation_id)

    def _raise_final(self):
        if self._interrupt is not None:
            error, self._interrupt = self._interrupt, None
            raise error
        if self._primary is not None:
            code = self._primary
            if (code is ErrorCode.UOW_CLOSE_FAILED and self._commit is CommitStatus.SUCCEEDED):
                code = SpecialErrorCode.UOW_COMMITTED_CLEANUP_FAILED
            raise UowError(code, self._outcome) from None
