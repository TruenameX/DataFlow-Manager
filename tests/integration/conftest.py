"""Opt-in integration fixtures; import and collection never open connections."""

import importlib.util
import ipaddress
import json
import logging
import os
from pathlib import Path
from uuid import UUID

import pytest
from _pytest.logging import catching_logs
from dataflow_manager.db.session import _POOL_LOGGER_NAME


_original_capture_enter = None


def _enter_without_pool_capture(capture):
    root = logging.getLogger()
    if capture.level is not None:
        capture.handler.setLevel(capture.level)
    root.addHandler(capture.handler)
    capture.attached_loggers.append(root)
    for logger in tuple(root.manager.loggerDict.values()):
        if (isinstance(logger, logging.Logger) and not logger.propagate
                and logger is not root and logger.name != _POOL_LOGGER_NAME):
            logger.addHandler(capture.handler)
            capture.attached_loggers.append(logger)
    if capture.level is not None:
        capture.orig_level = root.level
        root.setLevel(min(capture.orig_level, capture.level))
    return capture.handler


def pytest_configure(config):
    global _original_capture_enter
    if _original_capture_enter is None:
        _original_capture_enter = catching_logs.__enter__
        catching_logs.__enter__ = _enter_without_pool_capture


def pytest_unconfigure(config):
    global _original_capture_enter
    if _original_capture_enter is not None:
        catching_logs.__enter__ = _original_capture_enter
        _original_capture_enter = None


def _load(name):
    alias = "_task005_integration_" + name
    spec = importlib.util.spec_from_file_location(alias, Path(__file__).parents[1] / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    if name == "task005_readiness":
        import sys
        previous = sys.modules.get(alias)
        sys.modules[alias] = module
        try:
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                del sys.modules[alias]
            else:
                sys.modules[alias] = previous
    else:
        spec.loader.exec_module(module)
    return module


def prepare_resources(environment, factory):
    """Attest and validate before factory; no physical connection here."""
    readiness, profile = _load("task005_readiness"), _load("task005_profile")
    readiness.require_real_integration_gate(
        opt_in=environment.get("DFM_TEST_INTEGRATION") == "YES",
        owner_approved=environment.get("DFM_TEST_OWNER_APPROVED") == "YES",
        isolated_server=environment.get("DFM_TEST_ISOLATED_SERVER") == "YES",
        sql_approved=environment.get("DFM_TEST_SQL_APPROVED") == "YES",
        inventory_verified=environment.get("DFM_TEST_INVENTORY_VERIFIED") == "YES",
    )
    readiness.require_safe_driver_logging()
    expected_server_address(environment)
    excluded = environment.get("DFM_TEST_EXCLUDED_RESOURCES", "")
    if not excluded or any(not item for item in excluded.split(",")):
        raise RuntimeError("TASK005_INTEGRATION_NOT_AUTHORIZED")
    ledger_path(environment)
    config = profile.validate_profile(
        environment, approved=True,
        approved_database=environment.get("DFM_TEST_APPROVED_DATABASE"),
        approved_test_role=environment.get("DFM_TEST_APPROVED_TEST_ROLE"),
        preparation_role=environment.get("DFM_TEST_PREPARATION_ROLE"),
        excluded_resources=frozenset(excluded.split(",")),
    )
    try:
        run_id = UUID(environment.get("DFM_TEST_RUN_ID", ""))
        plan = readiness.plan_resources(run_id, excluded_resources=frozenset(excluded.split(",")))
        if (config.name, config.user, environment.get("DFM_TEST_PREPARATION_ROLE")) != (
                plan.database, plan.test_role, plan.preparation_role):
            raise ValueError("resource identity mismatch")
        if ledger_path(environment).name != "task005_ledger_" + run_id.hex + ".jsonl":
            raise ValueError("ledger identity mismatch")
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("TASK005_INTEGRATION_NOT_AUTHORIZED") from None
    return config, factory(config)


def ledger_path(environment):
    value = environment.get("DFM_TEST_LEDGER_PATH")
    if type(value) is not str or not value:
        raise RuntimeError("TASK005_LEDGER_REJECTED")
    path = Path(value)
    allowed = Path(__file__).parent.resolve()
    if (path.parent.resolve() != allowed or not path.name.startswith("task005_ledger_")
            or not path.name.endswith(".jsonl")):
        raise RuntimeError("TASK005_LEDGER_REJECTED")
    return path


def record_probe(environment, database, probe_id, status):
    """Append non-secret intended/committed row identities to a run ledger."""
    from uuid import UUID
    try:
        parsed = UUID(hex=probe_id)
        if parsed.version != 4 or parsed.hex != probe_id or status not in ("intended", "committed"):
            raise ValueError("invalid probe")
        if database != environment.get("DFM_TEST_APPROVED_DATABASE"):
            raise ValueError("invalid database")
        path = ledger_path(environment)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        payload = (json.dumps({"database": database, "probe_id": probe_id, "status": status}) + "\n").encode()
        fd = os.open(path, flags, 0o600)
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError("partial ledger write")
        finally:
            os.close(fd)
    except Exception:
        raise RuntimeError("TASK005_LEDGER_FAILED") from None


def protect_driver_logging(engine):
    from sqlalchemy import event
    readiness = _load("task005_readiness")
    def check_before_connect(dialect, connection_record, cargs, cparams):
        readiness.require_safe_driver_logging()
    event.listen(engine, "do_connect", check_before_connect)


def expected_server_address(environment):
    try:
        value = environment.get("DFM_TEST_EXPECTED_SERVER_ADDRESS")
        address = ipaddress.ip_address(value)
        if address.version != 4 or str(address) != value:
            raise ValueError("invalid address")
        return value
    except (TypeError, ValueError):
        raise RuntimeError("TASK005_SERVER_IDENTITY_REJECTED") from None


def verify_identity(engine, config, environment):
    """After authorized connection, fail closed before application test SQL."""
    from sqlalchemy import text
    address = expected_server_address(environment)
    try:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT current_database(), current_user, "
                "current_setting('server_version_num')::integer, "
                "host(inet_server_addr()), inet_server_port()"
            )).one()
            if tuple(row) != (config.name, config.user, 180006, address, 5432):
                raise ValueError("identity mismatch")
    except Exception:
        raise RuntimeError("TASK005_SERVER_IDENTITY_REJECTED") from None


def dispose_safely(engine):
    try:
        engine.dispose()
    except Exception:
        raise RuntimeError("TASK005_ENGINE_DISPOSE_FAILED") from None


@pytest.fixture
def task005_real_integration():
    if os.environ.get("DFM_TEST_INTEGRATION") != "YES":
        pytest.skip("TASK-005 integration requires explicit authorization")
    from dataflow_manager.db.session import _create_intermediate_resources
    config, resources = prepare_resources(os.environ, _create_intermediate_resources)
    engine, maker = resources
    try:
        protect_driver_logging(engine)
        verify_identity(engine, config, os.environ)
        yield config, engine, maker
    finally:
        dispose_safely(engine)
