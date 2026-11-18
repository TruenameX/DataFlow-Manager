"""Validated request schemas and explicit, secret-free response projection."""

from datetime import datetime, timezone
from ipaddress import IPv6Address, ip_address
import re
import unicodedata
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema


_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
_NUMERIC_QUAD = re.compile(r"[0-9]+(?:\.[0-9]+){3}\Z", re.ASCII)
_FIELDS = ("id", "name", "host", "port", "database", "username", "password", "request")


def _utf8(value: str) -> bytes:
    try:
        data = value.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError("invalid value") from None
    if "\x00" in value:
        raise ValueError("invalid value")
    return data


def _controls(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def _name(value: str) -> str:
    _utf8(value)
    if not 1 <= len(value) <= 255 or value.startswith(" ") or value.endswith(" ") or _controls(value):
        raise ValueError("invalid name")
    key = unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())
    if len(key.encode("utf-8")) > 1024:
        raise ValueError("invalid name")
    return value


def _host(value: str) -> str:
    _utf8(value)
    if not value or len(value) > 253 or not value.isascii() or any(c in value for c in ("%", "[", "]")):
        raise ValueError("invalid host")
    try:
        address = ip_address(value)
    except ValueError:
        if _NUMERIC_QUAD.fullmatch(value):
            raise ValueError("invalid host") from None
        host = value.lower()
        if any(not _HOST_LABEL.fullmatch(part) for part in host.split(".")):
            raise ValueError("invalid host") from None
        return host
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return "::ffff:" + str(address.ipv4_mapped)
    return str(address)


def _literal(value: str, *, database: bool) -> str:
    data = _utf8(value)
    if not 1 <= len(data) <= 63 or _controls(value):
        raise ValueError("invalid value")
    if database and ("=" in value or value.startswith(("postgres://", "postgresql://"))):
        raise ValueError("invalid value")
    return value


def _password(value: str) -> str:
    if not 1 <= len(_utf8(value)) <= 4096:
        raise ValueError("invalid password")
    return value


class _SourceFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("name", check_fields=False)
    @classmethod
    def validate_name(cls, value):
        return _name(value)

    @field_validator("host", check_fields=False)
    @classmethod
    def validate_host(cls, value):
        return _host(value)

    @field_validator("database", check_fields=False)
    @classmethod
    def validate_database(cls, value):
        return _literal(value, database=True)

    @field_validator("username", check_fields=False)
    @classmethod
    def validate_username(cls, value):
        return _literal(value, database=False)

    @field_validator("password", check_fields=False)
    @classmethod
    def validate_password(cls, value):
        return _password(value)


class CreateSource(_SourceFields):
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=63)
    username: str = Field(min_length=1, max_length=63)
    password: str = Field(repr=False, min_length=1, max_length=4096, json_schema_extra={"writeOnly": True})


class PatchSource(_SourceFields):
    name: Annotated[str, Field(min_length=1, max_length=255)] | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    host: Annotated[str, Field(min_length=1, max_length=253)] | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    port: Annotated[int, Field(ge=1, le=65535)] | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    database: Annotated[str, Field(min_length=1, max_length=63)] | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    username: Annotated[str, Field(min_length=1, max_length=63)] | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    password: Annotated[str, Field(min_length=1, max_length=4096)] | SkipJsonSchema[None] = Field(default_factory=lambda: None, repr=False, json_schema_extra={"writeOnly": True})

    @model_validator(mode="before")
    @classmethod
    def require_nonempty_object(cls, value):
        if not isinstance(value, dict) or not value:
            raise ValueError("invalid patch")
        return value

    @field_validator("name", "host", "port", "database", "username", "password", mode="before")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("null is not allowed")
        return value


def validate_source_id(value: str) -> UUID:
    try:
        if type(value) is not str:
            raise ValueError
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
        return parsed
    except (ValueError, AttributeError, TypeError):
        raise ValueError("invalid id") from None


class SourceRead(BaseModel):
    """Only the eight explicitly allowed response fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str
    host: str
    port: int
    database: str
    username: str
    created_at: datetime
    updated_at: datetime

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value):
        if type(value) is UUID:
            value = str(value)
        validate_source_id(value)
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_datetime(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("invalid timestamp")
        return value

    @field_serializer("created_at", "updated_at", when_used="json")
    def utc_timestamp(self, value):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def safe_validation_fields(errors) -> list[str]:
    """Project validation locations to the ordered public allowlist only."""
    found = set()
    for error in errors:
        location = error.get("loc", ())
        kind = error.get("type")
        if kind == "extra_forbidden":
            found.add("request")
            continue
        field = next((part for part in location if isinstance(part, str) and part in _FIELDS[:-1]), None)
        found.add(field if field is not None else "request")
    return [field for field in _FIELDS if field in found]
