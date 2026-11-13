"""Offline declarations/security only: no test executes a PostgreSQL CHECK."""

from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import uuid

import pytest
from sqlalchemy import CheckConstraint, Computed, Integer, PrimaryKeyConstraint, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.schema import CreateTable

from dataflow_manager.db.models import Base, DataSource


TABLE = DataSource.__table__
COLUMNS = (
    "id", "name", "name_key", "host", "port", "database_name", "username",
    "password_encrypted", "created_at", "updated_at",
)
CHECK_NAMES = {
    "ck_data_sources_name_nonempty", "ck_data_sources_name_length",
    "ck_data_sources_name_edges", "ck_data_sources_name_controls",
    "ck_data_sources_name_key_bytes", "ck_data_sources_host", "ck_data_sources_port",
    "ck_data_sources_database_bytes", "ck_data_sources_database_literal",
    "ck_data_sources_username_bytes", "ck_data_sources_username_controls",
    "ck_data_sources_password_token_nonempty",
}
KEY_SQL = "normalize(casefold(normalize(name, NFC) COLLATE pg_catalog.pg_unicode_fast), NFC)"


def compact(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


@pytest.fixture
def ddl() -> str:
    # Pure compilation: do not construct an Engine or execute the returned text.
    return str(CreateTable(TABLE).compile(dialect=postgresql.dialect()))


def test_table_identity_columns_and_nullability() -> None:
    assert TABLE.fullname == "public.data_sources"
    assert set(Base.metadata.tables) == {"public.data_sources"}
    assert tuple(TABLE.columns.keys()) == COLUMNS
    assert all(column.nullable is False for column in TABLE.columns)
    assert not TABLE.foreign_keys


def test_column_types_and_python_types() -> None:
    assert isinstance(TABLE.c.id.type, UUID)
    assert TABLE.c.id.type.as_uuid is True
    assert TABLE.c.id.type.python_type is uuid.UUID
    assert isinstance(TABLE.c.port.type, Integer)
    assert TABLE.c.port.type.python_type is int
    for name in ("name", "name_key", "host", "database_name", "username", "password_encrypted"):
        assert isinstance(TABLE.c[name].type, Text)
        assert TABLE.c[name].type.python_type is str
    for name in ("created_at", "updated_at"):
        column = TABLE.c[name]
        assert isinstance(column.type, TIMESTAMP)
        assert column.type.timezone is True
        assert column.type.precision == 6
        assert column.type.python_type is datetime


def test_uuid_application_default_without_server_generation() -> None:
    column = TABLE.c.id
    assert column.default is not None and column.default.is_callable
    assert column.server_default is None
    # SQLAlchemy wraps callable defaults to accept an execution context.
    first, second = column.default.arg(None), column.default.arg(None)
    assert isinstance(first, uuid.UUID) and first.version == 4
    assert isinstance(second, uuid.UUID) and second.version == 4
    assert first != second
    assert first.variant == uuid.RFC_4122
    assert set(TABLE.primary_key.columns.keys()) == {"id"}
    assert TABLE.primary_key.name == "pk_data_sources"


def test_timestamp_defaults_and_no_automatic_updates() -> None:
    for name in ("created_at", "updated_at"):
        column = TABLE.c[name]
        assert column.default is None
        assert str(column.server_default.arg) == "transaction_timestamp()"
    for column in TABLE.columns:
        assert column.onupdate is None
        if column.name != "name_key":
            assert column.server_onupdate is None
    # A Computed is a SQLAlchemy server-generated marker, not a timestamp hook.
    assert TABLE.c.name_key.server_onupdate is TABLE.c.name_key.computed
    assert TABLE.c.name_key.server_default is TABLE.c.name_key.computed


def test_generated_key_has_exact_server_expression(ddl: str) -> None:
    column = TABLE.c.name_key
    assert isinstance(column.computed, Computed)
    assert column.computed.persisted is True
    assert compact(str(column.computed.sqltext)) == KEY_SQL
    assert column.type.collation == "C"
    assert column.default is None
    assert 'name_key TEXT COLLATE "C" GENERATED ALWAYS AS (' in ddl
    assert ") STORED NOT NULL" in ddl
    assert KEY_SQL in compact(ddl)


def test_named_constraints_and_no_duplicate_unique_index(ddl: str) -> None:
    checks = [c for c in TABLE.constraints if isinstance(c, CheckConstraint)]
    assert {c.name for c in checks} == CHECK_NAMES
    assert len(checks) == len(CHECK_NAMES)
    assert len(TABLE.constraints) == len(CHECK_NAMES) + 2
    unique, = [c for c in TABLE.constraints if isinstance(c, UniqueConstraint)]
    assert unique.name == "uq_data_sources_name_key"
    assert tuple(unique.columns.keys()) == ("name_key",)
    assert unique.deferrable is False
    assert isinstance(TABLE.primary_key, PrimaryKeyConstraint)
    assert not TABLE.indexes
    assert ddl.count("UNIQUE (name_key) NOT DEFERRABLE") == 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("name_nonempty", "char_length(name) > 0"),
        ("name_length", "char_length(name) <= 255"),
        ("name_edges", '(left(name, 1) COLLATE "C") <> \' \' AND (right(name, 1) COLLATE "C") <> \' \''),
        ("name_key_bytes", "octet_length(name_key) <= 1024"),
        ("port", "port BETWEEN 1 AND 65535"),
        ("database_bytes", "octet_length(database_name) BETWEEN 1 AND 63"),
        ("username_bytes", "octet_length(username) BETWEEN 1 AND 63"),
        ("password_token_nonempty", "char_length(password_encrypted) > 0"),
    ],
)
def test_declared_simple_check_predicates(name: str, expected: str) -> None:
    check, = [c for c in TABLE.constraints if c.name == f"ck_data_sources_{name}"]
    assert compact(str(check.sqltext)) == expected


def test_name_edges_uses_c_collation_on_both_sides_in_ddl(ddl: str) -> None:
    expected = '(left(name, 1) COLLATE "C") <> \' \' AND (right(name, 1) COLLATE "C") <> \' \''
    assert f"CONSTRAINT ck_data_sources_name_edges CHECK ({expected})" in compact(ddl)
    assert "left(name, 1) <> ' ' AND right(name, 1) <> ' '" not in ddl


@pytest.mark.parametrize(
    ("constraint", "column"),
    [("name_controls", "name"), ("database_literal", "database_name"),
     ("username_controls", "username")],
)
def test_declared_exact_control_set(constraint: str, column: str) -> None:
    check, = [c for c in TABLE.constraints if c.name == f"ck_data_sources_{constraint}"]
    sql = str(check.sqltext)
    escapes, = re.findall(r"U&'([^']*)'", sql)
    codes = [int(value, 16) for value in re.findall(r"\\([0-9A-F]{4})", escapes)]
    assert codes == [*range(1, 32), 127]
    assert f"octet_length(translate({column}," in sql
    assert f"= octet_length({column})" in sql
    assert "[:cntrl:]" not in sql and "chr(0)" not in sql.lower()
    assert all(ord(character) >= 32 for character in sql)
    if column == "database_name":
        assert "position('=' in database_name) = 0" in sql
        assert "'^(postgres|postgresql)://'" in sql


def test_host_declaration_guards_cast_and_declares_full_profile(ddl: str) -> None:
    check, = [c for c in TABLE.constraints if c.name == "ck_data_sources_host"]
    sql = compact(str(check.sqltext))
    assert sql.startswith("octet_length(host) BETWEEN 1 AND 253 AND CASE WHEN")
    assert "WHEN pg_catalog.pg_input_is_valid(host, 'pg_catalog.inet') THEN" in sql
    assert '(host COLLATE "C") = (pg_catalog.host(CAST(host AS pg_catalog.inet)) COLLATE "C")' in sql
    assert sql.count("CAST(") == 1
    assert sql.index("THEN") < sql.index("CAST(") < sql.index("ELSE")
    assert sql.endswith("END")
    assert "(host COLLATE \"C\") !~ '^[0-9]+([.][0-9]+){3}$'" in sql
    # The LDH alternative enforces label boundaries, lowercase and label size.
    assert "'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?([.][a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'" in sql
    assert ddl.count("CAST(host AS pg_catalog.inet)") == 1
    # These assertions establish declaration/guard placement, not DB execution.


def test_compiled_ddl_has_no_unapproved_mechanisms(ddl: str) -> None:
    normalized = compact(ddl).lower()
    assert normalized.startswith("create table public.data_sources (")
    for forbidden in ("lower(name)", "citext", "nfkc", "trigger", "on update", "chr(0)", "::inet"):
        assert forbidden not in normalized
    assert normalized.count("cast(") == 1  # the CASE-guarded host cast only
    assert "uuid not null" in normalized
    assert normalized.count("default transaction_timestamp()") == 2
    assert normalized.count("timestamp(6) with time zone") == 2
    assert "clock_timestamp" not in normalized


def test_no_plaintext_or_automatic_serialization_and_safe_repr() -> None:
    token = "synthetic-encrypted-token-for-repr-test"
    source = DataSource(name="Источник", password_encrypted=token)
    assert source.name == "Источник"
    assert source.password_encrypted == token
    assert token not in repr(source)
    assert "Источник" not in repr(source)
    assert DataSource.__repr__ is object.__repr__
    assert not {"password", "token", "key"}.intersection(TABLE.columns.keys())
    for serializer in ("model_dump", "dict", "to_dict", "__iter__"):
        assert not hasattr(source, serializer)
    assert source.name_key is None  # no client-side normalizer/default
    assert not hasattr(DataSource.name_key, "fset")


def test_default_writes_omit_generated_key() -> None:
    # Compile Core statements only; all supplied values are synthetic.
    insert = TABLE.insert().values(
        name="Name", host="localhost", port=5432, database_name="example",
        username="example", password_encrypted="synthetic-token",
    )
    compiled = str(insert.compile(dialect=postgresql.dialect()))
    inserted_columns = compiled.split("(", 1)[1].split(")", 1)[0]
    assert "name_key" not in inserted_columns
    update = str(TABLE.update().values(name="NAME").compile(dialect=postgresql.dialect()))
    assert "name_key" not in update
    # Explicit malicious assignments are rejected by GENERATED ALWAYS in DB,
    # not by an offline constructor test; live bypass tests remain D01/TASK-004.


def test_fresh_import_has_no_io_or_application_side_effects() -> None:
    script = textwrap.dedent("""
        import builtins
        import importlib
        import json
        import os
        import socket
        import sqlalchemy
        import sqlalchemy.engine
        import sqlalchemy.orm
        import sqlalchemy.pool
        from unittest.mock import patch
        from dataflow_manager.main import app

        routes_before = [(r.path, sorted(r.methods or [])) for r in app.routes]
        original_open = builtins.open
        def guarded_open(file, *args, **kwargs):
            name = os.fsdecode(file) if isinstance(file, (str, bytes, os.PathLike)) else ''
            if name.rsplit('/', 1)[-1] in {'.env', 'credentials'}:
                raise AssertionError('secret read during import')
            return original_open(file, *args, **kwargs)

        def forbidden(*args, **kwargs):
            raise AssertionError('runtime I/O or resource creation during import')

        with patch.object(sqlalchemy, 'create_engine', forbidden), \\
             patch.object(sqlalchemy.engine, 'create_engine', forbidden), \\
             patch.object(sqlalchemy.engine.Engine, '__init__', forbidden), \\
             patch.object(sqlalchemy.orm.Session, '__init__', forbidden), \\
             patch.object(sqlalchemy.orm, 'sessionmaker', forbidden), \\
             patch.object(sqlalchemy.pool.Pool, '__init__', forbidden), \\
             patch.object(socket, 'socket', forbidden), \\
             patch.object(socket, 'getaddrinfo', forbidden), \\
             patch.object(builtins, 'open', guarded_open), \\
             patch.object(os, 'getenv', forbidden):
            module = importlib.import_module('dataflow_manager.db.models')
            assert set(module.Base.metadata.tables) == {'public.data_sources'}
        assert routes_before == [(r.path, sorted(r.methods or [])) for r in app.routes]
        print(json.dumps({'import': 'safe', 'routes': 'unchanged'}))
    """)
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"import": "safe", "routes": "unchanged"}
