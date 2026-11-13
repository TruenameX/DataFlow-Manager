"""Initial data_sources declaration; downgrade requires a separate policy.

Revision ID: 0001_data_sources
Revises: None
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_data_sources"
down_revision = None
branch_labels = None
depends_on = None

_KEY = """normalize(
    casefold(
        normalize(name, NFC)
        COLLATE pg_catalog.pg_unicode_fast
    ),
    NFC
)"""
_CONTROLS = "U&'" + "".join(f"\\{n:04X}" for n in (*range(1, 32), 127)) + "'"


def _no_controls(column: str) -> str:
    return f"octet_length(translate({column}, {_CONTROLS}, '')) = octet_length({column})"


_HOST = r"""octet_length(host) BETWEEN 1 AND 253
AND CASE
    WHEN pg_catalog.pg_input_is_valid(host, 'pg_catalog.inet') THEN
        (host COLLATE "C") =
        (pg_catalog.host(CAST(host AS pg_catalog.inet)) COLLATE "C")
    ELSE
        (host COLLATE "C") ~
        '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?([.][a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
END"""


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(collation="C"), sa.Computed(_KEY, persisted=True), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database_name", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True, precision=6), server_default=sa.text("transaction_timestamp()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True, precision=6), server_default=sa.text("transaction_timestamp()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_data_sources"),
        sa.UniqueConstraint("name_key", name="uq_data_sources_name_key", deferrable=False),
        sa.CheckConstraint("char_length(name) > 0", name="ck_data_sources_name_nonempty"),
        sa.CheckConstraint("char_length(name) <= 255", name="ck_data_sources_name_length"),
        sa.CheckConstraint("(left(name, 1) COLLATE \"C\") <> ' ' AND (right(name, 1) COLLATE \"C\") <> ' '", name="ck_data_sources_name_edges"),
        sa.CheckConstraint(_no_controls("name"), name="ck_data_sources_name_controls"),
        sa.CheckConstraint("octet_length(name_key) <= 1024", name="ck_data_sources_name_key_bytes"),
        sa.CheckConstraint(_HOST, name="ck_data_sources_host"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ck_data_sources_port"),
        sa.CheckConstraint("octet_length(database_name) BETWEEN 1 AND 63", name="ck_data_sources_database_bytes"),
        sa.CheckConstraint(_no_controls("database_name") + " AND position('=' in database_name) = 0" + " AND (database_name COLLATE \"C\") !~ '^(postgres|postgresql)://'", name="ck_data_sources_database_literal"),
        sa.CheckConstraint("octet_length(username) BETWEEN 1 AND 63", name="ck_data_sources_username_bytes"),
        sa.CheckConstraint(_no_controls("username"), name="ck_data_sources_username_controls"),
        sa.CheckConstraint("char_length(password_encrypted) > 0", name="ck_data_sources_password_token_nonempty"),
        schema="public",
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicitly approved data-loss policy")
