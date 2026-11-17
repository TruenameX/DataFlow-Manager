"""Repository operations using a caller-owned transactional UoW.

The caller owns commit, rollback and close. ORM objects are internal persistence
objects and must not be serialized directly into public responses. Materialize
the permitted response snapshot before commit, then publish only after success:
the Session can expire and detach ORM instances at commit/close.
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from dataflow_manager.db.models import DataSource
from dataflow_manager.db.uow import ErrorCode, LifecycleState, UowError


class SourceNameConflict(UowError):
    """Known unique-name conflict; capture inside UoW before final UowError.

    A service may retain this typed fact inside the context, then use it only
    after checking the final UoW outcome; never retain raw driver exceptions.
    """

    def __init__(self):
        super().__init__(ErrorCode.UOW_EXECUTION_FAILED)


def _name_conflict(error: IntegrityError) -> bool:
    original = error.orig
    diag = getattr(original, "diag", None)
    if diag is None:
        return False
    code = getattr(original, "sqlstate", None)
    if code is None:
        code = getattr(diag, "sqlstate", None)
    schema = getattr(diag, "schema_name", None)
    table = getattr(diag, "table_name", None)
    return (code == "23505"
            and getattr(diag, "constraint_name", None) == "uq_data_sources_name_key"
            and schema in (None, "public")
            and table in (None, "data_sources"))


_FIELDS = frozenset(("name", "host", "port", "database_name", "username", "password_encrypted"))


class SourceRepository:
    """Create, read, update and delete within the caller's current UoW."""

    def __init__(self, uow):
        self._uow = uow

    def _guard(self):
        uow = self._uow
        if uow.state is not LifecycleState.ACTIVE:
            return uow._invalid()

    def _run(self, operation):
        self._guard()
        uow = self._uow
        try:
            return operation(uow._session)
        except Exception as error:
            uow._record(ErrorCode.UOW_EXECUTION_FAILED)
            uow._state = LifecycleState.FAILED
            try:
                conflict = isinstance(error, IntegrityError) and _name_conflict(error)
            except Exception:
                conflict = False
            except BaseException as interrupt:
                uow._interrupted(interrupt)
                raise
            if conflict:
                raise SourceNameConflict() from None
            raise UowError(ErrorCode.UOW_EXECUTION_FAILED) from None
        except BaseException as error:
            uow._interrupted(error)
            uow._state = LifecycleState.FAILED
            raise

    def create(self, *, name, host, port, database_name, username, password_encrypted):
        def operation(session):
            item = DataSource(name=name, host=host, port=port, database_name=database_name,
                              username=username, password_encrypted=password_encrypted)
            session.add(item)
            session.flush()
            return item
        return self._run(operation)

    def get(self, source_id):
        return self._run(lambda session: session.get(DataSource, source_id))

    def list(self):
        return self._run(lambda session: list(session.scalars(select(DataSource)).all()))

    def update(self, source_id, changes):
        self._guard()
        if type(changes) is not dict or not changes or not changes.keys() <= _FIELDS:
            raise ValueError("invalid source changes")
        def operation(session):
            item = session.scalars(select(DataSource).where(DataSource.id == source_id).with_for_update().execution_options(populate_existing=True)).first()
            if item is None:
                return None
            changed = any(key == "password_encrypted" or getattr(item, key) != value
                          for key, value in changes.items())
            if not changed:
                return item
            for key, value in changes.items():
                setattr(item, key, value)
            item.updated_at = func.clock_timestamp()
            session.flush()
            return item
        return self._run(operation)

    def delete(self, source_id):
        def operation(session):
            item = session.scalars(select(DataSource).where(DataSource.id == source_id).with_for_update()).first()
            if item is None:
                return False
            session.delete(item)
            session.flush()
            return True
        return self._run(operation)
