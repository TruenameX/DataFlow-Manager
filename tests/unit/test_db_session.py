import logging
import socket
import subprocess
import sys
import textwrap
import traceback
from types import SimpleNamespace
import uuid

import pytest
import psycopg

from dataflow_manager.db import session
from dataflow_manager.db.config import ConfigError, DatabaseConfig


@pytest.fixture(scope="session", autouse=True)
def isolate_pool_logger_from_pytest_capture():
    """Keep pytest's automatic capture on root and other loggers, not the pool."""
    from _pytest.logging import catching_logs

    original_enter = catching_logs.__enter__

    def enter_without_pool_capture(capture):
        root = logging.getLogger()
        if capture.level is not None:
            capture.handler.setLevel(capture.level)
        root.addHandler(capture.handler)
        capture.attached_loggers.append(root)
        for logger in tuple(root.manager.loggerDict.values()):
            if (isinstance(logger, logging.Logger) and not logger.propagate
                    and logger is not root and logger.name != session._POOL_LOGGER_NAME):
                logger.addHandler(capture.handler)
                capture.attached_loggers.append(logger)
        if capture.level is not None:
            capture.orig_level = root.level
            root.setLevel(min(capture.orig_level, capture.level))
        return capture.handler

    catching_logs.__enter__ = enter_without_pool_capture
    try:
        yield original_enter
    finally:
        catching_logs.__enter__ = original_enter


def fake_pq(implementation="binary", version=160000, keywords=(b"require_auth",)):
    def parse(conninfo):
        assert conninfo == b"require_auth=scram-sha-256"
        return [SimpleNamespace(keyword=keyword) for keyword in keywords]

    return SimpleNamespace(
        __impl__=implementation,
        version=lambda: version,
        Conninfo=SimpleNamespace(parse=parse),
    )


def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def dbapi_spy(monkeypatch):
    calls = []

    def blocked(*args, **kwargs):
        calls.append("connect")
        raise AssertionError("DBAPI connect is forbidden")

    monkeypatch.setattr(psycopg, "connect", blocked)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(blocked))
    return calls


def test_dbapi_spy_positive_control(dbapi_spy):
    with pytest.raises(AssertionError):
        psycopg.connect()
    with pytest.raises(AssertionError):
        psycopg.Connection.connect()
    assert len(dbapi_spy) == 2


@pytest.mark.parametrize("implementation", ["python", "c", "binary"])
@pytest.mark.parametrize("version", [150000, 160000])
def test_supported_client_without_network(monkeypatch, implementation, version, dbapi_spy):
    forbid_network(monkeypatch)
    monkeypatch.setattr(session, "_load_pq", lambda: fake_pq(implementation, version))
    result = session.check_libpq_capability()
    assert result.implementation == implementation
    assert result.version == version
    assert not dbapi_spy


@pytest.mark.parametrize("implementation,version,keywords", [
    ("unknown", 160000, (b"require_auth",)),
    (None, 160000, (b"require_auth",)),
    ("binary", 0, (b"require_auth",)),
    ("binary", -1, (b"require_auth",)),
    ("binary", 150000, ()),
    ("binary", True, (b"require_auth",)),
    ("binary", None, (b"require_auth",)),
    ("binary", 160000, ()),
    ("binary", 160000, (b"other",)),
])
def test_unknown_or_unsupported_client_fails_closed(monkeypatch, implementation, version, keywords, dbapi_spy):
    forbid_network(monkeypatch)
    monkeypatch.setattr(session, "_load_pq", lambda: fake_pq(implementation, version, keywords))
    with pytest.raises(session.ClientUnsupportedError) as caught:
        session.check_libpq_capability()
    assert caught.value.code == "DB_CLIENT_UNSUPPORTED"
    assert str(caught.value) == "DB_CLIENT_UNSUPPORTED"
    assert not dbapi_spy


@pytest.mark.parametrize("failure", ["load", "version", "parse"])
def test_api_failure_is_sanitized(monkeypatch, caplog, failure, dbapi_spy):
    forbid_network(monkeypatch)
    marker = f"secret-{uuid.uuid4().hex}"
    control = logging.Logger("capability-test-control", logging.WARNING)
    control.propagate = False
    control.addHandler(caplog.handler)
    control.warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()

    def fail(*args, **kwargs):
        raise RuntimeError(marker)

    pq = fake_pq()
    if failure == "load":
        monkeypatch.setattr(session, "_load_pq", fail)
    else:
        if failure == "version":
            pq.version = fail
        else:
            pq.Conninfo.parse = fail
        monkeypatch.setattr(session, "_load_pq", lambda: pq)

    with pytest.raises(session.ClientUnsupportedError) as caught:
        session.check_libpq_capability()
    assert caught.value.code == "DB_CLIENT_UNSUPPORTED"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    diagnostics = (
        str(caught.value), repr(caught.value), caplog.text,
        "".join(traceback.format_exception(caught.type, caught.value, caught.tb)),
    )
    assert all(marker not in value for value in diagnostics)
    assert not dbapi_spy


def test_installed_libpq_capability_api_without_network(monkeypatch, dbapi_spy):
    from psycopg import pq

    forbid_network(monkeypatch)
    try:
        options = pq.Conninfo.parse(b"require_auth=scram-sha-256")
        recognized = any(option.keyword == b"require_auth" for option in options)
    except Exception:
        recognized = False
    if not recognized or type(pq.version()) is not int or pq.version() <= 0 or pq.__impl__ not in ("python", "c", "binary"):
        with pytest.raises(session.ClientUnsupportedError):
            session.check_libpq_capability()
    else:
        result = session.check_libpq_capability()
        assert result.version == pq.version()
        assert result.implementation == pq.__impl__
    assert not dbapi_spy


def test_first_import_is_offline():
    script = textwrap.dedent("""
        import importlib
        import socket
        import sys

        class ForbiddenIO(Exception):
            pass

        def forbidden(*args, **kwargs):
            raise ForbiddenIO("blocked I/O")

        socket.socket = forbidden
        socket.create_connection = forbidden
        socket.getaddrinfo = forbidden
        if sys.argv[1] == "control":
            try:
                socket.socket()
            except ForbiddenIO:
                print("blocked")
                sys.exit(0)
            sys.exit("control failed")
        assert "dataflow_manager.db.session" not in sys.modules
        importlib.import_module("dataflow_manager.db.session")
        print("imported")
    """)
    for mode, expected in (("control", "blocked"), ("import", "imported")):
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, mode],
            capture_output=True, text=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
        assert not result.stderr


@pytest.fixture
def db_config(monkeypatch):
    for name in tuple(__import__("os").environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)
    return DatabaseConfig(
        host="127.0.0.1", port=5432, name="task005_test", user="test_user",
        password=" synthetic secret @:/ ", pool_size=2, max_overflow=1,
        pool_timeout=5, connect_timeout=3, pool_pre_ping=True, pool_recycle=1800,
        isolation_level="READ COMMITTED",
    )


@pytest.mark.parametrize("name,value", [("PGHOSTADDR", "127.0.0.2"), ("PG_UNKNOWN", "")])
def test_forbidden_environment_precedes_gate_and_engine(monkeypatch, db_config, name, value, dbapi_spy):
    import sqlalchemy

    monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(session, "check_libpq_capability", lambda: calls.append("gate"))
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: calls.append("engine"))
    with pytest.raises(ConfigError) as caught:
        session._create_intermediate_resources(db_config)
    assert caught.value.code == "DB_ENV_FORBIDDEN"
    assert calls == []
    assert not dbapi_spy


def test_gate_failure_precedes_engine(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy

    calls = []
    def fail_gate():
        raise session.ClientUnsupportedError()

    monkeypatch.setattr(session, "check_libpq_capability", fail_gate)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: calls.append("engine"))
    with pytest.raises(session.ClientUnsupportedError):
        session._create_intermediate_resources(db_config)
    assert calls == []
    assert not dbapi_spy


def test_guarded_resources_parameters_without_io(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy
    import sqlalchemy.orm

    forbid_network(monkeypatch)
    calls = []
    create_engine = sqlalchemy.create_engine
    def spy_engine(url, **kwargs):
        calls.append(("engine", url, kwargs))
        return create_engine(url, **kwargs)

    def forbidden_session(*args, **kwargs):
        raise AssertionError("Session must not be created")

    monkeypatch.setattr(session, "check_libpq_capability", lambda: calls.append(("gate",)))
    monkeypatch.setattr(sqlalchemy, "create_engine", spy_engine)
    monkeypatch.setattr(sqlalchemy.orm.Session, "__init__", forbidden_session)
    engine, factory = session._create_intermediate_resources(db_config)
    assert [call[0] for call in calls] == ["gate", "engine"]
    _, url, kwargs = calls[1]
    assert "creator" not in kwargs
    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database, url.username, url.password) == (
        db_config.host, db_config.port, db_config.name, db_config.user, db_config.password,
    )
    assert not url.query
    assert kwargs["connect_args"] == {
        "connect_timeout": 3, "sslmode": "disable", "gssencmode": "disable",
        "require_auth": "scram-sha-256",
    }
    assert {key: kwargs[key] for key in (
        "pool_size", "max_overflow", "pool_timeout", "pool_pre_ping", "pool_recycle",
        "isolation_level", "echo", "hide_parameters",
    )} == {
        "pool_size": 2, "max_overflow": 1, "pool_timeout": 5, "pool_pre_ping": True,
        "pool_recycle": 1800, "isolation_level": "READ COMMITTED", "echo": False,
        "hide_parameters": True,
    }
    cargs, cparams = engine.dialect.create_connect_args(url)
    assert not cargs
    assert isinstance(cparams.pop("context"), psycopg.adapt.AdaptersMap)
    assert cparams == {"host": db_config.host, "port": db_config.port,
                       "dbname": db_config.name, "user": db_config.user,
                       "password": db_config.password}
    assert factory.kw["bind"] is engine
    assert factory.kw["autobegin"] is False
    assert factory.kw["close_resets_only"] is False
    assert db_config.password not in repr(url)
    assert sqlalchemy.event.contains(engine, "do_connect", session._guard_connect)
    assert not dbapi_spy
    engine.dispose()


class FakeDBAPIStop(Exception):
    pass


@pytest.fixture
def observed_dbapi(monkeypatch):
    calls = []

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise FakeDBAPIStop("fake DBAPI stopped before network")

    # SQLAlchemy's installed DefaultDialect.connect calls loaded_dbapi.connect.
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(fake_connect))
    return calls


def test_fake_dbapi_positive_control(observed_dbapi):
    with pytest.raises(FakeDBAPIStop):
        psycopg.connect()
    assert len(observed_dbapi) == 1


def test_first_connect_checks_environment_before_dbapi(monkeypatch, db_config, observed_dbapi):
    forbid_network(monkeypatch)
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    order = []
    check = session._check_environment

    def observe_check():
        order.append("environment")
        check()

    monkeypatch.setattr(session, "_check_environment", observe_check)
    engine, _ = session._create_intermediate_resources(db_config)
    assert order == ["environment"]
    assert not observed_dbapi
    with pytest.raises(FakeDBAPIStop):
        engine.connect()
    assert order == ["environment", "environment"]
    assert len(observed_dbapi) == 1
    args, params = observed_dbapi[0]
    assert args == ()
    assert set(params) == {"host", "port", "dbname", "user", "password", "connect_timeout",
                           "sslmode", "gssencmode", "require_auth", "context"}
    assert isinstance(params["context"], psycopg.adapt.AdaptersMap)
    assert {key: value for key, value in params.items() if key != "context"} == {
        "host": db_config.host, "port": db_config.port, "dbname": db_config.name,
        "user": db_config.user, "password": db_config.password, "connect_timeout": 3,
        "sslmode": "disable", "gssencmode": "disable", "require_auth": "scram-sha-256",
    }
    engine.dispose()


@pytest.mark.parametrize("forbidden", [
    {"PGHOSTADDR": "127.0.0.2"}, {"PG_UNRECOGNIZED": "value"},
    {"PG_EMPTY": ""}, {"PG_FIRST": "", "PG_SECOND": "value"},
])
def test_connect_rechecks_all_pg_names_before_dbapi(monkeypatch, db_config, observed_dbapi, forbidden, caplog):
    forbid_network(monkeypatch)
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    engine, _ = session._create_intermediate_resources(db_config)
    for key, value in forbidden.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError) as caught:
        engine.connect()
    assert caught.value.code == "DB_ENV_FORBIDDEN"
    assert str(caught.value) == "DB_ENV_FORBIDDEN"
    assert not observed_dbapi
    diagnostics = (str(caught.value), repr(caught.value), repr(vars(caught.value)), caplog.text,
                   "".join(traceback.format_exception(caught.type, caught.value, caught.tb)))
    assert all(db_config.password not in item for item in diagnostics)
    engine.dispose()


def test_registration_occurs_before_factory_return(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy
    import sqlalchemy.orm

    calls = []
    register = sqlalchemy.event.listen
    make_sessionmaker = sqlalchemy.orm.sessionmaker
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)

    def observe(engine, name, handler, *args, **kwargs):
        if name == "do_connect":
            calls.append((engine, name, handler))
        return register(engine, name, handler, *args, **kwargs)

    def observe_sessionmaker(*args, **kwargs):
        assert sqlalchemy.event.contains(kwargs["bind"], "do_connect", session._guard_connect)
        return make_sessionmaker(*args, **kwargs)

    monkeypatch.setattr(sqlalchemy.event, "listen", observe)
    monkeypatch.setattr(sqlalchemy.orm, "sessionmaker", observe_sessionmaker)
    engine, factory = session._create_intermediate_resources(db_config)
    assert calls == [(engine, "do_connect", session._guard_connect)]
    assert sqlalchemy.event.contains(engine, "do_connect", session._guard_connect)
    assert factory.kw["bind"] is engine
    assert not dbapi_spy
    engine.dispose()


def test_registration_cleanup_logging_failure_preserves_primary(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy

    class FakeEngine:
        def dispose(self):
            raise RuntimeError("cleanup failure")

    def fail(*args, **kwargs):
        raise RuntimeError("failure")

    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(session, "_verify_engine_pool", lambda *args: None)
    monkeypatch.setattr(sqlalchemy.event, "listen", fail)
    monkeypatch.setattr(session.logging.getLogger(session.__name__), "error", fail)
    with pytest.raises(session.EngineInitError) as caught:
        session._create_intermediate_resources(db_config)
    assert caught.value.code == "DB_ENGINE_INIT_FAILED"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not dbapi_spy


@pytest.mark.parametrize("dispose_fails", [False, True])
def test_registration_failure_cleans_up_and_sanitizes(monkeypatch, db_config, dbapi_spy, caplog, dispose_fails):
    import sqlalchemy
    from dataclasses import replace

    marker = f"secret-{uuid.uuid4().hex}"
    db_config = replace(db_config, password=marker)
    control = logging.Logger("registration-control", logging.WARNING)
    control.propagate = False
    control.addHandler(caplog.handler)
    control.warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()
    calls = []

    class FakeEngine:
        def dispose(self):
            calls.append("dispose")
            if dispose_fails:
                raise RuntimeError(marker)

    def fail_registration(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(session, "_verify_engine_pool", lambda *args: None)
    monkeypatch.setattr(sqlalchemy.event, "listen", fail_registration)
    with pytest.raises(session.EngineInitError) as caught:
        session._create_intermediate_resources(db_config)
    assert calls == ["dispose"]
    assert caught.value.code == "DB_ENGINE_INIT_FAILED"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert [record.getMessage() for record in caplog.records] == (
        ["DB_DISPOSE_FAILED"] if dispose_fails else []
    )
    assert all(record.exc_info is None for record in caplog.records)
    diagnostics = (str(caught.value), repr(caught.value), repr(vars(caught.value)), caplog.text,
                   "".join(traceback.format_exception(caught.type, caught.value, caught.tb)))
    assert all(marker not in item for item in diagnostics)
    assert not dbapi_spy


def test_sessionmaker_and_dispose_failure_are_sanitized(
    monkeypatch, db_config, caplog, dbapi_spy
):
    import sqlalchemy
    import sqlalchemy.orm

    forbid_network(monkeypatch)
    marker = db_config.password
    control = logging.Logger("dispose-test-control", logging.WARNING)
    control.propagate = False
    control.addHandler(caplog.handler)
    control.warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()

    calls = []

    class FailingEngine:
        def dispose(self):
            calls.append("dispose")
            raise RuntimeError(marker)

    def fail_sessionmaker(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(
        sqlalchemy, "create_engine", lambda *args, **kwargs: FailingEngine()
    )
    monkeypatch.setattr(session, "_verify_engine_pool", lambda *args: None)
    monkeypatch.setattr(sqlalchemy.event, "listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(sqlalchemy.orm, "sessionmaker", fail_sessionmaker)

    with pytest.raises(session.EngineInitError) as caught:
        session._create_intermediate_resources(db_config)

    assert calls == ["dispose"]
    assert caught.value.code == "DB_ENGINE_INIT_FAILED"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert [record.getMessage() for record in caplog.records] == [
        "DB_DISPOSE_FAILED"
    ]
    assert all(record.exc_info is None for record in caplog.records)
    diagnostics = (
        str(caught.value),
        repr(caught.value),
        caplog.text,
        "".join(traceback.format_exception(caught.type, caught.value, caught.tb)),
    )
    assert all(marker not in value for value in diagnostics)
    assert not dbapi_spy


@pytest.mark.parametrize("phase", ["engine", "sessionmaker"])
def test_factory_failure_is_sanitized(monkeypatch, db_config, caplog, phase, dbapi_spy):
    import sqlalchemy
    import sqlalchemy.orm

    marker = db_config.password
    control = logging.Logger("engine-test-control", logging.WARNING)
    control.propagate = False
    control.addHandler(caplog.handler)
    control.warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    def fail(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(sqlalchemy if phase == "engine" else sqlalchemy.orm,
                        "create_engine" if phase == "engine" else "sessionmaker", fail)
    with pytest.raises(session.EngineInitError) as caught:
        session._create_intermediate_resources(db_config)
    assert caught.value.code == "DB_ENGINE_INIT_FAILED"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    diagnostics = (str(caught.value), repr(caught.value), caplog.text,
                   "".join(traceback.format_exception(caught.type, caught.value, caught.tb)))
    assert all(marker not in item for item in diagnostics)
    assert not dbapi_spy


class FakePoolCursor:
    """Simulate the dialect ping locally; never execute SQL or use a socket."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement):
        if statement != ";":
            raise AssertionError("unexpected statement in offline pool harness")
        self.connection.pings += 1
        if self.connection.ping_failure:
            self.connection.broken = self.connection.ping_failure == "disconnect"
            raise psycopg.OperationalError("synthetic offline ping failure")

    def close(self):
        pass


class FakePoolConnection:
    def __init__(self):
        self.autocommit = False
        self.isolation_level = None
        self.closed = False
        self.broken = False
        self.rollbacks = 0
        self.pings = 0
        self.ping_failure = None

    def add_notice_handler(self, handler):
        pass

    def cursor(self):
        return FakePoolCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def offline_pool(monkeypatch, db_config):
    """Real Engine/pool and guard, fake DBAPI; dialect initialization is stubbed."""
    forbid_network(monkeypatch)
    created = []
    checks = []
    check_environment = session._check_environment

    def observed_check():
        checks.append("check")
        check_environment()

    def fake_connect(*args, **kwargs):
        connection = FakePoolConnection()
        created.append(connection)
        return connection

    def forbidden_class_connect(*args, **kwargs):
        raise AssertionError("real psycopg connection path is forbidden")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(forbidden_class_connect))
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(session, "_check_environment", observed_check)
    engine, _ = session._create_intermediate_resources(db_config)
    # PostgreSQL server discovery and hstore introspection are outside this offline test.
    monkeypatch.setattr(engine.dialect, "initialize", lambda connection: None)
    try:
        yield engine, created, checks
    finally:
        engine.dispose()


def test_pool_first_and_reused_checkout_offline(offline_pool):
    engine, created, checks = offline_pool
    assert not created
    assert len(checks) == 1  # startup
    with engine.connect() as first:
        first_dbapi = first.connection.dbapi_connection
        assert len(created) == 1 and first_dbapi is created[0]
    assert not first_dbapi.closed
    with engine.connect() as second:
        assert second.connection.dbapi_connection is first_dbapi
    assert len(created) == 1
    assert len(checks) == 2  # no new physical connection on reuse


@pytest.mark.parametrize("replacement", ["invalidate", "recycle"])
@pytest.mark.parametrize("forbidden", [None, {"PGHOSTADDR": "127.0.0.2"},
                                        {"PG_UNKNOWN": "value"}, {"PG_EMPTY": ""},
                                        {"PG_FIRST": "", "PG_SECOND": "value"}])
def test_pool_replacement_rechecks_pg_before_dbapi(offline_pool, monkeypatch, replacement, forbidden):
    engine, created, checks = offline_pool
    connection = engine.connect()
    original = connection.connection.dbapi_connection
    assert len(created) == 1 and len(checks) == 2
    if replacement == "invalidate":
        connection.invalidate()
        connection.close()
    else:
        record = connection.connection._connection_record
        record.starttime -= engine.pool._recycle + 1
        connection.close()
    if forbidden is not None:
        for name, value in forbidden.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError) as caught:
            engine.connect()
        assert caught.value.code == "DB_ENV_FORBIDDEN"
        assert len(created) == 1
    else:
        with engine.connect() as replacement_connection:
            assert replacement_connection.connection.dbapi_connection is created[1]
        assert len(created) == 2
        assert created[1] is not original
    assert original.closed
    assert len(checks) == 3


def test_successful_pre_ping_reuses_connection(offline_pool):
    engine, created, checks = offline_pool
    with engine.connect() as first:
        original = first.connection.dbapi_connection
        assert original.pings == 0  # fresh connection skips pre-ping
    with engine.connect() as second:
        assert second.connection.dbapi_connection is original
    assert original.pings == 1
    assert len(created) == 1
    assert len(checks) == 2


@pytest.mark.parametrize("forbidden", [None, {"PGHOSTADDR": "127.0.0.2"},
                                        {"PG_UNKNOWN": "value"}, {"PG_EMPTY": ""},
                                        {"PG_FIRST": "", "PG_SECOND": "value"}])
def test_failed_pre_ping_automatic_replacement(offline_pool, monkeypatch, forbidden):
    import os
    import sqlalchemy

    engine, created, checks = offline_pool
    invalidations = []
    sqlalchemy.event.listen(engine.pool, "invalidate",
                            lambda connection, record, error: invalidations.append((connection, error)))
    with engine.connect() as first:
        original = first.connection.dbapi_connection
    assert len(created) == 1 and len(checks) == 2 and original.pings == 0
    original.ping_failure = "disconnect"
    if forbidden is not None:
        for name, value in forbidden.items():
            monkeypatch.setenv(name, value)
    environment_before = dict(os.environ)
    if forbidden is None:
        with engine.connect() as replacement:
            assert replacement.connection.dbapi_connection is created[1]
        assert len(created) == 2 and created[1] is not original
    else:
        with pytest.raises(ConfigError) as caught:
            engine.connect()
        assert caught.value.code == "DB_ENV_FORBIDDEN"
        assert len(created) == 1
    assert dict(os.environ) == environment_before
    assert original.pings == 1 and original.broken and original.closed
    assert len(invalidations) == 1 and invalidations[0][0] is original
    assert len(checks) == 3


def test_ping_error_without_disconnect_does_not_replace(offline_pool):
    import sqlalchemy

    engine, created, checks = offline_pool
    invalidations = []
    sqlalchemy.event.listen(engine.pool, "invalidate",
                            lambda connection, record, error: invalidations.append(connection))
    with engine.connect() as first:
        original = first.connection.dbapi_connection
    original.ping_failure = "other"
    with pytest.raises(sqlalchemy.exc.DBAPIError) as caught:
        engine.connect()
    assert isinstance(caught.value.orig, psycopg.OperationalError)
    assert caught.value.connection_invalidated is False
    assert original.pings == 1 and not original.broken
    assert len(created) == 1 and len(checks) == 2


def test_pool_channel_is_ready_before_engine_and_reused(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy

    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    original = sqlalchemy.create_engine
    def create(*args, **kwargs):
        channel = logging.getLogger(session._POOL_LOGGER_NAME)
        assert channel.handlers == [session._pool_handler]
        assert channel.propagate is False and channel.level == logging.DEBUG
        assert kwargs["pool_logging_name"] == session._POOL_LOGGING_NAME
        assert kwargs["echo_pool"] is False
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlalchemy, "create_engine", create)
    foreign = logging.getLogger("sqlalchemy.pool.impl.QueuePool.foreign_test")
    before = (foreign.level, foreign.propagate, tuple(foreign.handlers), tuple(foreign.filters))
    first, _ = session._create_intermediate_resources(db_config)
    second, _ = session._create_intermediate_resources(db_config)
    channel = logging.getLogger(session._POOL_LOGGER_NAME)
    assert first.pool.logger is channel and second.pool.logger is channel
    assert len(channel.handlers) == 1
    assert (foreign.level, foreign.propagate, tuple(foreign.handlers), tuple(foreign.filters)) == before
    first.dispose()
    assert first.pool.logger is channel and channel.handlers == [session._pool_handler]
    second.dispose()
    assert not dbapi_spy


def test_pool_channel_sanitizes_real_invalidation_and_close_error(offline_pool, caplog):
    engine, created, _ = offline_pool
    marker = "pool-secret-" + uuid.uuid4().hex
    caplog.set_level(logging.DEBUG)
    control = logging.getLogger("pool-positive-control")
    control.warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()
    connection = engine.connect()
    original = connection.connection.dbapi_connection

    def fail_close():
        raise RuntimeError(marker)

    original.close = fail_close
    connection.invalidate(RuntimeError(marker))
    connection.close()
    records = [r for r in caplog.records if r.name == "dataflow_manager.db.pool_diagnostics"]
    assert {r.category for r in records} >= {"POOL_DIAGNOSTIC_EVENT", "POOL_DIAGNOSTIC_ERROR"}
    assert all(r.msg in {"Pool diagnostic event", "Pool diagnostic error"} for r in records)
    assert all(not r.args and r.exc_info is None and r.stack_info is None for r in records)
    assert not any(r.name == session._POOL_LOGGER_NAME for r in caplog.records)
    assert marker not in caplog.text
    assert len(created) == 1


def test_pool_channel_conflict_fails_before_engine(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy

    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    session._configure_pool_diagnostics()
    channel = logging.getLogger(session._POOL_LOGGER_NAME)
    foreign = logging.NullHandler()
    channel.addHandler(foreign)
    calls = []
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: calls.append("engine"))
    try:
        with pytest.raises(session.EngineInitError) as caught:
            session._create_intermediate_resources(db_config)
        assert caught.value.code == "DB_ENGINE_INIT_FAILED"
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert foreign in channel.handlers
        assert not calls and not dbapi_spy
    finally:
        channel.removeHandler(foreign)


def test_pool_channel_setup_failure_is_sanitized(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy

    marker = "pool-setup-" + uuid.uuid4().hex
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    def fail():
        raise RuntimeError(marker)
    monkeypatch.setattr(session, "_configure_pool_diagnostics", fail)
    calls = []
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: calls.append("engine"))
    with pytest.raises(session.EngineInitError) as caught:
        session._create_intermediate_resources(db_config)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert marker not in "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert not calls and not dbapi_spy


def test_pytest_capture_is_not_trusted(isolate_pool_logger_from_pytest_capture, monkeypatch, db_config, dbapi_spy, caplog):
    from _pytest.logging import catching_logs, LogCaptureHandler
    from dataclasses import replace
    import sqlalchemy

    marker = "pytest-capture-secret-" + uuid.uuid4().hex
    db_config = replace(db_config, password=marker)
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    session._configure_pool_diagnostics()
    channel = logging.getLogger(session._POOL_LOGGER_NAME)
    capture = catching_logs(LogCaptureHandler())
    original_enter = isolate_pool_logger_from_pytest_capture
    original_enter(capture)
    try:
        assert capture.handler in channel.handlers
        calls = []
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: calls.append(1))
        with pytest.raises(session.EngineInitError) as caught:
            session._create_intermediate_resources(db_config)
        assert caught.value.code == "DB_ENGINE_INIT_FAILED"
        assert marker not in caplog.text
        assert marker not in capture.handler.stream.getvalue()
        assert marker not in "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
        assert not calls and not dbapi_spy
    finally:
        capture.__exit__(None, None, None)
    assert channel.handlers == [session._pool_handler]


def test_pytest_capture_fixture_restores_original(isolate_pool_logger_from_pytest_capture):
    from _pytest.logging import catching_logs

    isolated_enter = catching_logs.__enter__
    original_enter = isolate_pool_logger_from_pytest_capture
    root = logging.getLogger()
    channel = logging.getLogger(session._POOL_LOGGER_NAME)
    root_before = tuple(root.handlers)
    channel_before = tuple(channel.handlers)
    catching_logs.__enter__ = original_enter
    try:
        fixture = isolate_pool_logger_from_pytest_capture_fixture()
        assert next(fixture) is original_enter
        assert catching_logs.__enter__ is not original_enter
        fixture.close()
        assert catching_logs.__enter__ is original_enter
        assert tuple(root.handlers) == root_before
        assert tuple(channel.handlers) == channel_before
    finally:
        catching_logs.__enter__ = isolated_enter


def isolate_pool_logger_from_pytest_capture_fixture():
    return isolate_pool_logger_from_pytest_capture.__wrapped__()


def test_pytest_capture_isolation_preserves_other_loggers(isolate_pool_logger_from_pytest_capture):
    from _pytest.logging import catching_logs

    root = logging.getLogger()
    other = logging.Logger("nonpropagating-control")
    other.propagate = False
    # Use a registered logger to exercise pytest's loggerDict traversal.
    other = logging.getLogger("capture-isolation-control")
    previous = other.propagate
    other.propagate = False
    session._configure_pool_diagnostics()
    channel = logging.getLogger(session._POOL_LOGGER_NAME)
    before_root = tuple(root.handlers)
    before_other = tuple(other.handlers)
    before_channel = tuple(channel.handlers)
    capture = catching_logs(logging.NullHandler())
    try:
        with capture:
            assert capture.handler in root.handlers
            assert capture.handler in other.handlers
            assert tuple(channel.handlers) == before_channel
        assert tuple(root.handlers) == before_root
        assert tuple(other.handlers) == before_other
        assert tuple(channel.handlers) == before_channel
    finally:
        other.propagate = previous


def test_unverified_pool_is_not_disposed_via_foreign_logger(monkeypatch, db_config, dbapi_spy, caplog):
    import sqlalchemy

    marker = "foreign-pool-" + uuid.uuid4().hex
    calls = []
    class ForeignEngine:
        pool = object()
        def dispose(self):
            calls.append("dispose")
            logging.getLogger("foreign-pool").error(marker)

    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: ForeignEngine())
    with pytest.raises(session.EngineInitError) as caught:
        session._create_intermediate_resources(db_config)
    assert caught.value.code == "DB_ENGINE_INIT_FAILED"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert not calls and not dbapi_spy
    assert marker not in caplog.text
    assert [r.getMessage() for r in caplog.records] == ["POOL_CLEANUP_SKIPPED_UNVERIFIED"]
    assert all(r.exc_info is None and r.stack_info is None for r in caplog.records)


@pytest.mark.parametrize("close", [True, False])
def test_dispose_preserves_old_checked_out_and_new_pool_diagnostics(offline_pool, caplog, close):
    engine, created, checks = offline_pool
    marker = "lifecycle-secret-" + uuid.uuid4().hex
    caplog.set_level(logging.DEBUG)
    logging.getLogger("lifecycle-control").warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()
    connection = engine.connect()
    old_pool = engine.pool
    old_dbapi = connection.connection.dbapi_connection
    channel = old_pool.logger
    handler = session._pool_handler
    engine.dispose(close=close)
    assert engine.pool is not old_pool
    assert engine.pool.logger is old_pool.logger is channel
    assert channel.handlers == [handler] and channel.propagate is False
    assert not old_dbapi.closed
    connection.invalidate(RuntimeError(marker))
    connection.close()
    assert old_dbapi.closed
    with engine.connect() as replacement:
        assert replacement.connection.dbapi_connection is created[1]
        replacement.invalidate(RuntimeError(marker))
    assert len(created) == 2 and len(checks) == 3
    records = [r for r in caplog.records if r.name == "dataflow_manager.db.pool_diagnostics"]
    assert len(records) >= 2
    assert all(not r.args and r.exc_info is None and r.stack_info is None for r in records)
    assert marker not in caplog.text
    assert not any(r.name == session._POOL_LOGGER_NAME for r in caplog.records)


def test_concurrent_engine_creation_keeps_one_channel(monkeypatch, db_config, dbapi_spy):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    forbid_network(monkeypatch)
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    barrier = threading.Barrier(4, timeout=5)
    channel = session._configure_pool_diagnostics()
    foreign = logging.getLogger("sqlalchemy.pool.impl.QueuePool.lifecycle_foreign")
    before = (foreign.level, foreign.propagate, tuple(foreign.handlers), tuple(foreign.filters))

    def create():
        barrier.wait()
        engine, factory = session._create_intermediate_resources(db_config)
        assert engine.pool.logger is channel and factory.kw["bind"] is engine
        return engine

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(create) for _ in range(4)]
        engines = [future.result(timeout=10) for future in futures]
    assert channel.handlers == [session._pool_handler] and channel.propagate is False
    assert (foreign.level, foreign.propagate, tuple(foreign.handlers), tuple(foreign.filters)) == before
    assert not dbapi_spy
    for engine in engines:
        engine.dispose()


def test_concurrent_first_channel_setup_is_atomic(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    name = "sqlalchemy.pool.impl.QueuePool.offline_first_" + uuid.uuid4().hex
    logger = logging.getLogger(name)
    monkeypatch.setattr(session, "_POOL_LOGGER_NAME", name)
    monkeypatch.setattr(session, "_pool_handler", None)
    barrier = threading.Barrier(4, timeout=5)

    def configure():
        barrier.wait()
        result = session._configure_pool_diagnostics()
        assert result.propagate is False and result.level == logging.DEBUG
        assert result.handlers == [session._pool_handler]
        return result

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(configure) for _ in range(4)]
            assert all(f.result(timeout=10) is logger for f in futures)
        assert len(logger.handlers) == 1
    finally:
        logger.removeHandler(session._pool_handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


@pytest.mark.parametrize("conflict", ["level", "propagate", "logger_filter", "handler_filter", "handler_level"])
def test_channel_configuration_conflicts_fail_closed(monkeypatch, db_config, dbapi_spy, conflict):
    import sqlalchemy

    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    channel = session._configure_pool_diagnostics()
    handler = session._pool_handler
    calls = []
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: calls.append("engine"))
    if conflict == "level":
        monkeypatch.setattr(channel, "level", logging.WARNING)
    elif conflict == "propagate":
        monkeypatch.setattr(channel, "propagate", True)
    elif conflict == "logger_filter":
        monkeypatch.setattr(channel, "filters", [logging.Filter()])
    elif conflict == "handler_filter":
        monkeypatch.setattr(handler, "filters", [logging.Filter()])
    else:
        monkeypatch.setattr(handler, "level", logging.WARNING)
    with pytest.raises(session.EngineInitError) as caught:
        session._create_intermediate_resources(db_config)
    assert caught.value.code == "DB_ENGINE_INIT_FAILED"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert not calls and not dbapi_spy


def test_channel_handler_registration_failure_restores_clean_state(monkeypatch, db_config, dbapi_spy):
    import sqlalchemy

    name = "sqlalchemy.pool.impl.QueuePool.offline_failure_" + uuid.uuid4().hex
    logger = logging.getLogger(name)
    monkeypatch.setattr(session, "_POOL_LOGGER_NAME", name)
    monkeypatch.setattr(session, "_pool_handler", None)
    monkeypatch.setattr(session, "check_libpq_capability", lambda: None)
    calls = []
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: calls.append("engine"))
    original_add = logger.addHandler
    def fail(handler):
        assert logger.propagate is False and logger.level == logging.DEBUG
        original_add(handler)
        raise RuntimeError("synthetic handler failure")
    with monkeypatch.context() as context:
        context.setattr(logger, "addHandler", fail)
        with pytest.raises(session.EngineInitError) as caught:
            session._create_intermediate_resources(db_config)
    assert caught.value.code == "DB_ENGINE_INIT_FAILED"
    assert not calls and not dbapi_spy
    assert logger.handlers == [] and logger.level == logging.NOTSET and logger.propagate
    try:
        assert session._configure_pool_diagnostics() is logger
    finally:
        logger.removeHandler(session._pool_handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_adapter_failure_does_not_replace_pool_invalidation(offline_pool, monkeypatch):
    engine, created, _ = offline_pool
    original_get_logger = session.logging.getLogger
    def get_logger(name=None):
        if name == "dataflow_manager.db.pool_diagnostics":
            raise RuntimeError("synthetic diagnostic failure")
        return original_get_logger(name)
    connection = engine.connect()
    original = connection.connection.dbapi_connection
    monkeypatch.setattr(session.logging, "getLogger", get_logger)
    connection.invalidate(RuntimeError("synthetic disconnect"))
    connection.close()
    assert original.closed and len(created) == 1


def test_pool_reset_failure_is_sanitized(offline_pool, caplog):
    engine, created, _ = offline_pool
    marker = "reset-secret-" + uuid.uuid4().hex
    caplog.set_level(logging.DEBUG)
    logging.getLogger("reset-positive-control").error("%s", marker)
    assert marker in caplog.text
    caplog.clear()
    connection = engine.connect()
    original = connection.connection.dbapi_connection
    def fail_rollback():
        raise RuntimeError(marker)
    original.rollback = fail_rollback
    connection.close()
    records = [r for r in caplog.records if r.name == "dataflow_manager.db.pool_diagnostics"]
    assert any(r.category == "POOL_DIAGNOSTIC_ERROR" for r in records)
    assert all(not r.args and r.exc_info is None and r.stack_info is None for r in records)
    assert marker not in caplog.text
    assert not any(r.name == session._POOL_LOGGER_NAME for r in caplog.records)
    assert original.closed and len(created) == 1
