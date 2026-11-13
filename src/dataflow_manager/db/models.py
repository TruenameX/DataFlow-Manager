"""Offline SQLAlchemy metadata for the agreed TASK-002 schema.

No engine, session, encryption, or API serialization belongs in this module.
CHECK execution and PostgreSQL profile compatibility remain TASK-004 gates (D01–D04).
"""

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative metadata without connection or session configuration."""


_NAME_KEY_EXPRESSION = """normalize(
    casefold(
        normalize(name, NFC)
        COLLATE pg_catalog.pg_unicode_fast
    ),
    NFC
)"""

# Enumerate exactly U+0001–U+001F/U+007F, independent of locale. PostgreSQL
# UTF8 text cannot contain U+0000. Compare byte lengths, not collated strings.
_CONTROL_CHARACTERS = "U&'" + "".join(
    f"\\{codepoint:04X}" for codepoint in (*range(1, 32), 127)
) + "'"


def _without_controls(column: str) -> str:
    """Build metadata from fixed column names, never from user input."""
    return (
        f"octet_length(translate({column}, {_CONTROL_CHARACTERS}, '')) "
        f"= octet_length({column})"
    )


# inet_in supports PostgreSQL soft input errors. CASE (rather than AND
# evaluation order) guards the only cast of the column. host(inet) supplies
# PostgreSQL's canonical IP spelling, including IPv4-mapped IPv6. Hostnames
# use explicit ASCII LDH under C; no DNS lookup is performed. Exact execution
# of this declaration, including numeric LDH names/IP edge cases, is D01.
_HOST_CHECK = r"""octet_length(host) BETWEEN 1 AND 253
AND CASE
    WHEN pg_catalog.pg_input_is_valid(host, 'pg_catalog.inet') THEN
        (host COLLATE "C") =
        (pg_catalog.host(CAST(host AS pg_catalog.inet)) COLLATE "C")
    ELSE
        (host COLLATE "C") !~ '^[0-9]+([.][0-9]+){3}$'
        AND (host COLLATE "C") ~
        '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?([.][a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
END"""


class DataSource(Base):
    """Stored configuration; use a separate allowlisted Read DTO.

    The ordinary object repr intentionally exposes no mapped values.
    Generated name_key is server-owned: writers omit it from INSERT/UPDATE;
    PostgreSQL GENERATED ALWAYS rejects explicit non-DEFAULT assignments.
    Repository code will implement locking, no-op detection and explicit
    clock_timestamp() updates; the model has no automatic onupdate hook.
    """

    __tablename__ = "data_sources"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_data_sources"),
        UniqueConstraint(
            "name_key", name="uq_data_sources_name_key", deferrable=False
        ),
        CheckConstraint("char_length(name) > 0", name="ck_data_sources_name_nonempty"),
        CheckConstraint("char_length(name) <= 255", name="ck_data_sources_name_length"),
        CheckConstraint(
            "(left(name, 1) COLLATE \"C\") <> ' ' AND (right(name, 1) COLLATE \"C\") <> ' '",
            name="ck_data_sources_name_edges",
        ),
        CheckConstraint(_without_controls("name"), name="ck_data_sources_name_controls"),
        CheckConstraint(
            "octet_length(name_key) <= 1024", name="ck_data_sources_name_key_bytes"
        ),
        CheckConstraint(_HOST_CHECK, name="ck_data_sources_host"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_data_sources_port"),
        CheckConstraint(
            "octet_length(database_name) BETWEEN 1 AND 63",
            name="ck_data_sources_database_bytes",
        ),
        CheckConstraint(
            _without_controls("database_name")
            + " AND position('=' in database_name) = 0"
            + " AND (database_name COLLATE \"C\") !~ '^(postgres|postgresql)://'",
            name="ck_data_sources_database_literal",
        ),
        CheckConstraint(
            "octet_length(username) BETWEEN 1 AND 63",
            name="ck_data_sources_username_bytes",
        ),
        CheckConstraint(
            _without_controls("username"), name="ck_data_sources_username_controls"
        ),
        CheckConstraint(
            "char_length(password_encrypted) > 0",
            name="ck_data_sources_password_token_nonempty",
        ),
        {"schema": "public"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_key: Mapped[str] = mapped_column(
        Text(collation="C"),
        Computed(_NAME_KEY_EXPRESSION, persisted=True),
        nullable=False,
    )
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True, precision=6),
        server_default=text("transaction_timestamp()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True, precision=6),
        server_default=text("transaction_timestamp()"),
        nullable=False,
    )
