"""Source operations with one explicit unit of work per invocation.

Only non-secret immutable snapshots leave this module. Neither raw driver
exceptions nor ORM instances are part of the public service interface.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, TimeoutError as PoolTimeoutError
import psycopg

from dataflow_manager.db.sources import SourceNameConflict, SourceRepository
from dataflow_manager.db.uow import CommitStatus, ErrorCode, UnitOfWork, UowError
from dataflow_manager.security.crypto import CryptoError


class ServiceError(RuntimeError):
    """A fixed public classification; exception context is not safe to log."""

    _CODES = frozenset(("source_not_found", "source_name_conflict", "operation_unavailable", "internal_error", "validation_error"))

    def __init__(self, code: str):
        if type(code) is not str or code not in self._CODES:
            raise ValueError("invalid service error")
        self.code = code
        self.fields = ("name",) if code == "validation_error" else ()
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    id: UUID
    name: str
    host: str
    port: int
    database: str
    username: str
    created_at: datetime
    updated_at: datetime


def _snapshot(source):
    return SourceSnapshot(id=source.id, name=source.name, host=source.host,
                          port=source.port, database=source.database_name,
                          username=source.username, created_at=source.created_at,
                          updated_at=source.updated_at)


def _is_db_unavailable(error):
    """Inspect the controlled preliminary context without retaining raw errors."""
    try:
        original = error.__context__
        if isinstance(original, PoolTimeoutError):
            return True
        if not isinstance(original, DBAPIError):
            return False
        underlying = original.orig
        state = getattr(underlying, "sqlstate", None)
        if state is not None:
            return type(state) is str and state.startswith("08")
        return (original.connection_invalidated is True
                or (isinstance(original, OperationalError)
                    and isinstance(underlying, psycopg.OperationalError)))
    except Exception:
        return False


def _is_name_check(error):
    try:
        original = error.__context__
        if not isinstance(original, IntegrityError):
            return False
        driver = original.orig
        diag = driver.diag
        return (getattr(driver, "sqlstate", None) == "23514"
                and getattr(diag, "constraint_name", None) in {
                    "ck_data_sources_name_nonempty", "ck_data_sources_name_length",
                    "ck_data_sources_name_edges", "ck_data_sources_name_controls",
                    "ck_data_sources_name_key_bytes"}
                and getattr(diag, "schema_name", None) in (None, "public")
                and getattr(diag, "table_name", None) in (None, "data_sources"))
    except Exception:
        return False


class SourceService:
    """Coordinate repository and crypto, without managing HTTP or input validation."""

    def __init__(self, session_factory, crypto):
        self._session_factory = session_factory
        self._crypto = crypto

    def _encrypted(self, password):
        if self._crypto is None:
            raise ServiceError("operation_unavailable")
        try:
            return self._crypto.encrypt(password)
        except CryptoError:
            raise ServiceError("internal_error") from None

    def _run(self, operation, *, write=False):
        if self._session_factory is None:
            raise ServiceError("operation_unavailable")
        conflict = False
        unavailable = False
        invalid_name = False
        business_code = None
        result = None
        try:
            with UnitOfWork(self._session_factory) as work:
                try:
                    result = operation(SourceRepository(work))
                except SourceNameConflict:
                    # Retain only the typed fact; UoW emits its final error on exit.
                    conflict = True
                except UowError as error:
                    # Q14 permits controlled internal inspection, never logging it.
                    unavailable = _is_db_unavailable(error)
                    invalid_name = _is_name_check(error)
                except ServiceError as error:
                    # Only a closed, non-secret classification survives cleanup.
                    business_code = error.code
                    work.mark_failed(ErrorCode.UOW_BUSINESS_ERROR)
                else:
                    if write and result is not None:
                        try:
                            work.commit()
                        except UowError as error:
                            unavailable = _is_db_unavailable(error)
                            raise
        except UowError as error:
            outcome = error.outcome
            if (business_code is not None and outcome is not None
                    and outcome.primary_error_code is ErrorCode.UOW_BUSINESS_ERROR):
                raise ServiceError(business_code) from None
            if (conflict and outcome is not None
                    and outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED
                    and outcome.commit_status is CommitStatus.NOT_ATTEMPTED):
                raise ServiceError("source_name_conflict") from None
            if (unavailable and outcome is not None
                    and outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED):
                raise ServiceError("operation_unavailable") from None
            if (invalid_name and outcome is not None
                    and outcome.primary_error_code is ErrorCode.UOW_EXECUTION_FAILED):
                raise ServiceError("validation_error") from None
            if unavailable or _is_db_unavailable(error):
                raise ServiceError("operation_unavailable") from None
            raise ServiceError("internal_error") from None
        return result

    def create(self, values):
        token = self._encrypted(values["password"])
        fields = {"name": values["name"], "host": values["host"], "port": values["port"],
                  "database_name": values["database"], "username": values["username"],
                  "password_encrypted": token}
        return self._run(lambda repo: _snapshot(repo.create(**fields)), write=True)

    def list(self):
        return self._run(lambda repo: [_snapshot(item) for item in repo.list()])

    def get(self, source_id):
        def read(repo):
            source = repo.get(source_id)
            return None if source is None else _snapshot(source)
        result = self._run(read)
        if result is None:
            raise ServiceError("source_not_found")
        return result

    def update(self, source_id, values):
        fields = {("database_name" if key == "database" else key): value
                  for key, value in values.items() if key != "password"}
        def update(repo):
            source = repo._get_for_update(source_id)
            if source is None:
                return None
            if "password" in values:
                fields["password_encrypted"] = self._encrypted(values["password"])
            source = repo._update_locked(source, fields)
            return None if source is None else _snapshot(source)
        result = self._run(update, write=True)
        if result is None:
            raise ServiceError("source_not_found")
        return result

    def delete(self, source_id):
        result = self._run(lambda repo: True if repo.delete(source_id) else None, write=True)
        if result is None:
            raise ServiceError("source_not_found")
