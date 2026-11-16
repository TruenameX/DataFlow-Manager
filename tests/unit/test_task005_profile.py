"""Offline-only checks; this module contains no integration tests or DB I/O."""

import importlib.util
from pathlib import Path
import socket

import pytest


_spec = importlib.util.spec_from_file_location("task005_profile", Path(__file__).parents[1] / "task005_profile.py")
profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profile)


@pytest.fixture
def env():
    return {"DFM_DB_HOST": "127.0.0.1", "DFM_DB_PORT": "5432", "DFM_DB_NAME": "task005_unique_123",
            "DFM_DB_USER": "task005_test_role", "DFM_DB_PASSWORD": "synthetic-profile-secret",
            "DFM_DB_POOL_SIZE": "2", "DFM_DB_MAX_OVERFLOW": "1", "DFM_DB_POOL_TIMEOUT": "5",
            "DFM_DB_CONNECT_TIMEOUT": "3", "DFM_DB_POOL_PRE_PING": "true",
            "DFM_DB_POOL_RECYCLE": "1800", "DFM_DB_ISOLATION_LEVEL": "READ COMMITTED"}


def check(env, **options):
    values = {"approved": True, "approved_database": "task005_unique_123",
              "preparation_role": "task005_prepare_role", "excluded_resources": frozenset({"task004_db"})}
    values.update(options)
    return profile.validate_profile(env, **values)


def test_approved_synthetic_snapshot_and_no_mutation(env, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("unexpected network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    before = env.copy()
    config = check(env)
    assert config.name == "task005_unique_123" and env == before
    assert "synthetic-profile-secret" not in repr(config)
    with pytest.raises(AssertionError): socket.socket.connect(None, ("synthetic", 0))


@pytest.mark.parametrize("name,value", [
    ("DFM_DB_HOST", "127.0.0.2"), ("DFM_DB_PORT", "5433"),
    ("DFM_DB_NAME", "production"), ("DFM_DB_POOL_SIZE", "3"),
    ("DFM_DB_MAX_OVERFLOW", "2"), ("DFM_DB_POOL_TIMEOUT", "4"),
    ("DFM_DB_CONNECT_TIMEOUT", "4"), ("DFM_DB_POOL_PRE_PING", "false"),
    ("DFM_DB_POOL_RECYCLE", "0"), ("DFM_DB_ISOLATION_LEVEL", "SERIALIZABLE"),
    ("PG_UNKNOWN", ""), ("PGHOSTADDR", "127.0.0.2"),
])
def test_rejects_bad_profile_and_forbidden_pg(env, name, value):
    env[name] = value
    with pytest.raises(ValueError, match="^TASK005_PROFILE_REJECTED$") as caught:
        check(env)
    assert "synthetic-profile-secret" not in repr(caught.value)


@pytest.mark.parametrize("options", [
    {"approved": False}, {"approved": 1}, {"approved_database": "task005_other"},
    {"preparation_role": "task005_test_role"}, {"preparation_role": "bad role"},
    {"excluded_resources": frozenset({"task005_unique_123"})},
    {"excluded_resources": frozenset({"task005_test_role"})},
    {"excluded_resources": ["task004_db"]},
])
def test_rejects_unapproved_or_overlapping_identity(env, options):
    with pytest.raises(ValueError, match="^TASK005_PROFILE_REJECTED$"):
        check(env, **options)


def test_missing_configuration_is_rejected_safely(env):
    del env["DFM_DB_PASSWORD"]
    with pytest.raises(ValueError, match="^TASK005_PROFILE_REJECTED$") as caught:
        check(env)
    assert "synthetic-profile-secret" not in str(caught.value)
