"""Opt-in integration fixtures; import and collection never open connections."""

import importlib.util
import logging
import os
from pathlib import Path

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
    excluded = environment.get("DFM_TEST_EXCLUDED_RESOURCES", "")
    if not excluded or any(not item for item in excluded.split(",")):
        raise RuntimeError("TASK005_INTEGRATION_NOT_AUTHORIZED")
    config = profile.validate_profile(
        environment, approved=True,
        approved_database=environment.get("DFM_TEST_APPROVED_DATABASE"),
        preparation_role=environment.get("DFM_TEST_PREPARATION_ROLE"),
        excluded_resources=frozenset(excluded.split(",")),
    )
    return config, factory(config)


def verify_identity(engine, config):
    """After authorized connection, fail closed before application test SQL."""
    from sqlalchemy import text
    try:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT current_database(), current_user, "
                "current_setting('server_version_num')::integer, "
                "host(inet_server_addr()), inet_server_port()"
            )).one()
            if tuple(row) != (config.name, config.user, 180006, "127.0.0.1", 5432):
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
        verify_identity(engine, config)
        yield config, engine, maker
    finally:
        dispose_safely(engine)
