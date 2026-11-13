from dataclasses import FrozenInstanceError
import logging
import subprocess
import sys
import textwrap
import traceback
import uuid

import pytest

from dataflow_manager.db.config import ConfigError, ConfigErrorCode, load_database_config


@pytest.fixture
def environment():
    return {
        "DFM_DB_HOST": "127.0.0.1", "DFM_DB_PORT": "5432",
        "DFM_DB_NAME": "task005_test", "DFM_DB_USER": "test_user",
        "DFM_DB_PASSWORD": "synthetic-secret-Ω ", "DFM_DB_POOL_SIZE": "2",
        "DFM_DB_MAX_OVERFLOW": "1", "DFM_DB_POOL_TIMEOUT": "5",
        "DFM_DB_CONNECT_TIMEOUT": "3", "DFM_DB_POOL_PRE_PING": "true",
        "DFM_DB_POOL_RECYCLE": "1800", "DFM_DB_ISOLATION_LEVEL": "READ COMMITTED",
    }


def assert_error(environment, code, caplog):
    with pytest.raises(ConfigError) as caught:
        load_database_config(environment)
    assert caught.value.code == code
    assert str(caught.value) == code.value
    assert "synthetic-secret" not in repr(caught.value)
    assert "synthetic-secret" not in caplog.text


@pytest.mark.parametrize("name", [
    "HOST", "PORT", "NAME", "USER", "PASSWORD", "POOL_SIZE", "MAX_OVERFLOW",
    "POOL_TIMEOUT", "CONNECT_TIMEOUT", "POOL_PRE_PING", "POOL_RECYCLE", "ISOLATION_LEVEL",
])
def test_all_fields_are_required(environment, name, caplog):
    del environment[f"DFM_DB_{name}"]
    assert_error(environment, ConfigErrorCode.DB_CONFIG_MISSING, caplog)


@pytest.mark.parametrize("name,valid,invalid", [
    ("PORT", ("1", "65535"), ("0", "65536")),
    ("POOL_SIZE", ("1", "19"), ("0", "21")),
    ("MAX_OVERFLOW", ("0", "18"), ("21",)),
    ("POOL_TIMEOUT", ("1", "60"), ("0", "61")),
    ("CONNECT_TIMEOUT", ("1", "30"), ("0", "31")),
    ("POOL_RECYCLE", ("0", "86400"), ("86401",)),
])
def test_numeric_boundaries(environment, name, valid, invalid, caplog):
    for value in valid:
        environment[f"DFM_DB_{name}"] = value
        load_database_config(environment)
    for value in invalid:
        environment[f"DFM_DB_{name}"] = value
        assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


@pytest.mark.parametrize("name", ["PORT", "POOL_SIZE", "MAX_OVERFLOW", "POOL_TIMEOUT", "CONNECT_TIMEOUT", "POOL_RECYCLE"])
@pytest.mark.parametrize("value", ["", "+1", "-1", " 1", "1 ", "01", "1.0", "1e0", "١", "9" * 5000])
def test_numbers_require_ascii_decimal(environment, name, value, caplog):
    environment[f"DFM_DB_{name}"] = value
    assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


def test_pool_total_limit(environment, caplog):
    environment.update(DFM_DB_POOL_SIZE="20", DFM_DB_MAX_OVERFLOW="0")
    assert load_database_config(environment).pool_size == 20
    environment["DFM_DB_MAX_OVERFLOW"] = "1"
    assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


@pytest.mark.parametrize("value", ["localhost", "::1", "/tmp", "127.0.0.1 ", "127.0.0.2"])
def test_host_is_exact(environment, value, caplog):
    environment["DFM_DB_HOST"] = value
    assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


@pytest.mark.parametrize("name", ["NAME", "USER"])
@pytest.mark.parametrize("value", ["", "A", "1abc", "a" * 64, "a-b", "a b", "a=b", "é", "postgres://x", "a\n"])
def test_identifier_rejects_invalid_values(environment, name, value, caplog):
    environment[f"DFM_DB_{name}"] = value
    assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


@pytest.mark.parametrize("value", ["a", "_", "a" * 63])
def test_identifier_accepts_boundaries(environment, value):
    environment.update(DFM_DB_NAME=value, DFM_DB_USER=value)
    assert load_database_config(environment).name == value


@pytest.mark.parametrize("value", ["", "bad\x00password", "\ud800"])
def test_invalid_password_is_safe(environment, value, caplog):
    environment["DFM_DB_PASSWORD"] = value
    assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


@pytest.mark.parametrize("kind", ["invalid_field", "nul_password", "surrogate_password", "empty_password"])
def test_actual_secret_does_not_reach_diagnostics(environment, kind, caplog):
    marker = f"secret-{uuid.uuid4().hex}"
    secret = {
        "invalid_field": marker,
        "nul_password": f"{marker}\x00tail",
        "surrogate_password": f"{marker}\ud800tail",
        "empty_password": "",
    }[kind]
    environment["DFM_DB_PASSWORD"] = secret
    if kind == "invalid_field":
        environment["DFM_DB_PORT"] = "invalid"

    # Empty strings cannot act as a leak detector; the marker verifies that
    # the capture is functional. Nonempty inputs also cover their escaped form.
    representations = {marker}
    if secret:
        representations.update((secret, secret.encode("unicode_escape").decode("ascii")))
    # Deliver the positive control exclusively to pytest's capture handler.
    logger = logging.Logger("task005a1-test-control", logging.WARNING)
    logger.propagate = False
    logger.addHandler(caplog.handler)
    for sensitive in representations:
        logger.warning("%s", sensitive)
        assert sensitive in caplog.text
    caplog.clear()
    assert not caplog.text

    with pytest.raises(ConfigError) as caught:
        load_database_config(environment)
    assert caught.value.code == ConfigErrorCode.DB_CONFIG_INVALID
    diagnostics = (
        str(caught.value), repr(caught.value), caplog.text,
        "".join(traceback.format_exception(caught.type, caught.value, caught.tb)),
    )
    assert str(caught.value) == ConfigErrorCode.DB_CONFIG_INVALID.value
    for rendered in diagnostics:
        for sensitive in representations:
            assert sensitive not in rendered


@pytest.mark.parametrize("name,value", [
    ("POOL_PRE_PING", " true"), ("POOL_PRE_PING", "yes"),
    ("ISOLATION_LEVEL", "READ  COMMITTED"), ("ISOLATION_LEVEL", "read commıtted"),
])
def test_boolean_and_isolation_reject_invalid_values(environment, name, value, caplog):
    environment[f"DFM_DB_{name}"] = value
    assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


def test_snapshot_is_immutable_and_redacts_password(environment):
    environment.update(DFM_DB_POOL_PRE_PING="FALSE", DFM_DB_ISOLATION_LEVEL="read committed")
    config = load_database_config(environment)
    assert config.password == "synthetic-secret-Ω "
    assert config.pool_pre_ping is False
    assert config.isolation_level == "READ COMMITTED"
    assert "synthetic-secret" in config.password  # positive control for the marker
    assert "synthetic-secret" not in repr(config)
    environment["DFM_DB_PASSWORD"] = "changed"
    assert config.password == "synthetic-secret-Ω "
    with pytest.raises(FrozenInstanceError):
        config.host = "changed"


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGPASSWORD", "PG_UNKNOWN", "PG"])
@pytest.mark.parametrize("value", ["", "forbidden-value"])
def test_any_pg_prefix_fails_closed(environment, name, value, caplog):
    environment[name] = value
    assert_error(environment, ConfigErrorCode.DB_ENV_FORBIDDEN, caplog)
    with pytest.raises(ConfigError) as caught:
        load_database_config(environment)
    assert name not in str(caught.value)
    if value:
        assert value not in repr(caught.value)
        assert value not in caplog.text


def test_non_string_field_is_rejected(environment, caplog):
    environment["DFM_DB_PORT"] = 5432
    assert_error(environment, ConfigErrorCode.DB_CONFIG_INVALID, caplog)


def test_validation_has_no_network_or_database_io(environment, monkeypatch):
    import socket
    import sqlalchemy
    from sqlalchemy.engine import Engine

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected I/O")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(sqlalchemy, "create_engine", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)
    assert load_database_config(environment).host == "127.0.0.1"


def test_fresh_import_has_no_network_or_database_io():
    script = textwrap.dedent("""
        import importlib
        import socket
        import sys
        import sqlalchemy
        from sqlalchemy.engine import Engine

        class ForbiddenIO(Exception):
            pass

        def forbidden(*args, **kwargs):
            raise ForbiddenIO("blocked I/O")

        socket.socket = forbidden
        socket.create_connection = forbidden
        socket.getaddrinfo = forbidden
        sqlalchemy.create_engine = forbidden
        Engine.connect = forbidden

        if sys.argv[1] == "control":
            try:
                socket.socket()
            except ForbiddenIO:
                print("control blocked")
                sys.exit(0)
            sys.exit("control failed to block I/O")

        assert "dataflow_manager.db.config" not in sys.modules
        importlib.import_module("dataflow_manager.db.config")
        print("import completed")
    """)
    for mode, expected in (("control", "control blocked"), ("import", "import completed")):
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, mode],
            capture_output=True, text=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
        assert not result.stderr
