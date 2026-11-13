"""Exclude invalid four-component numeric IPv4 candidates from DNS fallback.

Revision ID: 0002_host_policy
Revises: 0001_data_sources
"""
from alembic import op

revision = "0002_host_policy"
down_revision = "0001_data_sources"
branch_labels = None
depends_on = None

HOST_CHECK = r'''octet_length(host) BETWEEN 1 AND 253
AND CASE
    WHEN pg_catalog.pg_input_is_valid(host, 'pg_catalog.inet') THEN
        (host COLLATE "C") =
        (pg_catalog.host(CAST(host AS pg_catalog.inet)) COLLATE "C")
    ELSE
        (host COLLATE "C") !~ '^[0-9]+([.][0-9]+){3}$'
        AND (host COLLATE "C") ~
        '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?([.][a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
END'''


def upgrade() -> None:
    op.drop_constraint("ck_data_sources_host", "data_sources", schema="public", type_="check")
    op.create_check_constraint("ck_data_sources_host", "data_sources", HOST_CHECK, schema="public")


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicitly approved policy")
