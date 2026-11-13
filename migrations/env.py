"""Alembic environment: offline by default, online via an explicit connection."""
from alembic import context

from dataflow_manager.db.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="public",
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The caller must explicitly supply an already-authorized connection.
    # Never read environment files, create an engine, or choose a default URL.
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("An explicitly supplied migration connection is required")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
