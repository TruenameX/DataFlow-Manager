"""Offline migration checks: no server, engine, migration command or SQL execution."""
import importlib.util
import io
from pathlib import Path
import runpy
import socket

import pytest
from alembic import context
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from dataflow_manager.db.models import Base, DataSource

ROOT = Path(__file__).resolve().parents[2]
REVISION_PATH = ROOT / "migrations/versions/0001_data_sources.py"


def load_revision():
    spec = importlib.util.spec_from_file_location("initial_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network or engine connection is forbidden")
    from sqlalchemy.engine import Engine
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)


@pytest.fixture
def revision(no_network):
    return load_revision()


@pytest.fixture
def generated_sql(revision):
    output = io.StringIO()
    migration = MigrationContext.configure(
        dialect=postgresql.dialect(), opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(migration):
        revision.upgrade()  # Renders to memory; no Alembic upgrade command or DB.
    return output.getvalue()


def compact(value):
    return " ".join(value.split())


def test_single_root_and_secret_free_config(revision):
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0002_host_policy"]
    assert len(list(script.walk_revisions())) == 2
    assert revision.revision == "0001_data_sources"
    assert revision.down_revision is None
    assert config.get_main_option("sqlalchemy.url") is None
    assert "password" not in (ROOT / "alembic.ini").read_text().lower()


def test_environment_offline_and_missing_online_connection(monkeypatch, no_network):
    calls = []
    class Tx:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", lambda **kw: calls.append(kw))
    monkeypatch.setattr(context, "begin_transaction", lambda: Tx())
    monkeypatch.setattr(context, "run_migrations", lambda: calls.append("run"))
    namespace = runpy.run_path(str(ROOT / "migrations/env.py"))
    assert calls[0]["dialect_name"] == "postgresql"
    assert calls[0]["target_metadata"] is Base.metadata
    assert calls[0]["version_table_schema"] == "public"
    assert calls[1] == "run"
    class EmptyConfig:
        attributes = {}
    monkeypatch.setattr(context, "config", EmptyConfig(), raising=False)
    with pytest.raises(RuntimeError, match="explicitly supplied"):
        namespace["run_migrations_online"]()


def test_revision_matches_full_model_ddl(generated_sql, revision):
    expected = str(CreateTable(DataSource.__table__).compile(dialect=postgresql.dialect()))
    correction = runpy.run_path(str(ROOT / "migrations/versions/0002_host_policy.py"))
    assert compact(generated_sql).replace(compact(revision._HOST), compact(correction["HOST_CHECK"])).rstrip(";") == compact(expected)


def test_host_correction_only(no_network):
    correction = runpy.run_path(str(ROOT / "migrations/versions/0002_host_policy.py"))
    assert correction["revision"] == "0002_host_policy"
    assert correction["down_revision"] == "0001_data_sources"
    output = io.StringIO()
    migration = MigrationContext.configure(dialect=postgresql.dialect(), opts={"as_sql": True, "output_buffer": output})
    with Operations.context(migration):
        correction["upgrade"]()
    ddl = compact(output.getvalue())
    assert ddl.count("ALTER TABLE public.data_sources") == 2
    assert "DROP CONSTRAINT ck_data_sources_host" in ddl
    assert "ADD CONSTRAINT ck_data_sources_host CHECK" in ddl
    assert "(host COLLATE \"C\") !~ '^[0-9]+([.][0-9]+){3}$'" in ddl
    assert "DROP TABLE" not in ddl and "UPDATE " not in ddl and "INSERT " not in ddl
    model_check = next(c for c in DataSource.__table__.constraints if c.name == "ck_data_sources_host")
    assert compact(correction["HOST_CHECK"]) == compact(str(model_check.sqltext))
    with pytest.raises(NotImplementedError):
        correction["downgrade"]()


def test_independent_schema_invariants(generated_sql):
    sql = compact(generated_sql)
    assert sql.startswith("CREATE TABLE public.data_sources (")
    columns = ["id", "name", "name_key", "host", "port", "database_name", "username", "password_encrypted", "created_at", "updated_at"]
    before_constraints = sql.split("CONSTRAINT pk_data_sources", 1)[0]
    positions = [before_constraints.index(name + " ") for name in columns]
    assert positions == sorted(positions)
    assert before_constraints.count("NOT NULL") == 10
    assert "id UUID NOT NULL" in sql and "id UUID DEFAULT" not in sql
    assert 'name_key TEXT COLLATE "C" GENERATED ALWAYS AS' in sql
    assert "normalize(casefold(normalize(name, NFC) COLLATE pg_catalog.pg_unicode_fast), NFC)" in sql.replace("( ", "(").replace(" )", ")")
    assert "STORED NOT NULL" in sql
    assert "PRIMARY KEY (id)" in sql
    assert "uq_data_sources_name_key UNIQUE (name_key) NOT DEFERRABLE" in sql
    checks = ["name_nonempty", "name_length", "name_edges", "name_controls", "name_key_bytes", "host", "port", "database_bytes", "database_literal", "username_bytes", "username_controls", "password_token_nonempty"]
    for name in checks:
        assert f"CONSTRAINT ck_data_sources_{name} CHECK" in sql
    assert sql.count(" CHECK (") == 12
    assert '(left(name, 1) COLLATE "C") <> \' \' AND (right(name, 1) COLLATE "C") <> \' \'' in sql
    assert sql.count("TIMESTAMP(6) WITH TIME ZONE DEFAULT transaction_timestamp() NOT NULL") == 2
    assert "pg_catalog.pg_input_is_valid(host, 'pg_catalog.inet') THEN" in sql
    assert "CAST(host AS pg_catalog.inet)" in sql
    for forbidden in ("CREATE INDEX", "TRIGGER", "CITEXT", "lower(name)", " password TEXT", "DROP TABLE"):
        assert forbidden not in sql


def test_downgrade_not_implemented(revision):
    with pytest.raises(NotImplementedError):
        revision.downgrade()
