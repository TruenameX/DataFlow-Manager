from datetime import datetime, timedelta, timezone
from uuid import uuid1, uuid4

import pytest
from pydantic import ValidationError

from dataflow_manager.api.schemas import CreateSource, PatchSource, SourceRead, safe_validation_fields, validate_source_id


def valid():
    return dict(name="Name", host="EXAMPLE.com", port=5432, database="Пример", username=" User ", password="synthetic-secret")


def test_create_and_patch_share_canonicalization_and_missing_semantics():
    created = CreateSource.model_validate(valid())
    assert created.host == "example.com" and created.username == " User "
    assert "synthetic-secret" not in repr(created)
    patch = PatchSource.model_validate({"host": "EXAMPLE.com"})
    assert patch.model_dump(exclude_unset=True) == {"host": "example.com"}
    assert "password" not in patch.model_fields_set
    assert PatchSource.model_validate({"password": " "}).model_dump(exclude_unset=True) == {"password": " "}


@pytest.mark.parametrize("change", [
    {"name": ""}, {"name": " bad"}, {"name": "bad\x7f"}, {"name": "\ud800"},
    {"name": "x" * 256}, {"host": "999.1.1.1"}, {"host": "001.2.3.4"},
    {"host": "-invalid"}, {"host": "host."}, {"host": "[::1]"},
    {"host": "::1%lo0"}, {"host": "u\N{LATIN SMALL LETTER U WITH DIAERESIS}.com"},
    {"port": True}, {"port": 5432.0}, {"port": "5432"}, {"port": 0}, {"port": 65536},
    {"database": ""}, {"database": "a=b"}, {"database": "postgresql://host"},
    {"database": "я" * 32}, {"username": "\u007f"},
    {"password": ""}, {"password": "a\x00b"}, {"password": "🔒" * 1025},
    {"password": None}, {"id": "unapproved"},
])
def test_create_and_patch_reject_invalid_fields(change):
    with pytest.raises(ValidationError):
        CreateSource.model_validate(valid() | change)
    with pytest.raises(ValidationError):
        PatchSource.model_validate(change)


@pytest.mark.parametrize("value, expected", [("LOCALHOST", "localhost"), ("2001:0DB8::1", "2001:db8::1"),
                                              ("::ffff:192.0.2.1", "::ffff:192.0.2.1"), ("192.0.2.1", "192.0.2.1")])
def test_host_normalization(value, expected):
    assert PatchSource.model_validate({"host": value}).host == expected


def test_patch_requires_nonempty_object_and_forbids_null_for_each_field():
    for value in ({}, None, [], "string"):
        with pytest.raises(ValidationError):
            PatchSource.model_validate(value)
    for field in ("name", "host", "port", "database", "username", "password"):
        with pytest.raises(ValidationError):
            PatchSource.model_validate({field: None})


def test_path_id_is_canonical_uuid4():
    uid = uuid4()
    assert validate_source_id(str(uid)) == uid
    for value in (str(uid).upper(), uid.hex, "{" + str(uid) + "}", str(uuid1()), "bad"):
        with pytest.raises(ValueError):
            validate_source_id(value)


def test_safe_validation_projection_never_returns_input_or_unknown_location():
    marker = "synthetic-secret"
    try:
        CreateSource.model_validate(valid() | {"port": marker, marker: marker, "password": None})
    except ValidationError as error:
        fields = safe_validation_fields(error.errors())
    assert fields == ["port", "password", "request"] and marker not in repr(fields)
    assert safe_validation_fields([{"loc": ("path", "id"), "type": "invalid"},
                                   {"loc": ("query", marker), "type": "unexpected"}]) == ["id", "request"]


def test_read_projection_and_utc_timestamps_are_explicit():
    uid = uuid4()
    moment = datetime(2026, 11, 18, 12, 3, 4, 5, tzinfo=timezone(timedelta(hours=3)))
    result = SourceRead(id=uid, name="n", host="localhost", port=5432, database="db",
                        username="user", created_at=moment, updated_at=moment).model_dump(mode="json")
    assert set(result) == {"id", "name", "host", "port", "database", "username", "created_at", "updated_at"}
    assert result["id"] == str(uid)
    assert result["created_at"] == "2026-11-18T09:03:04.000005Z"
    with pytest.raises(ValidationError):
        SourceRead(**result, password="synthetic-secret")


def test_openapi_patch_fields_are_optional_but_not_nullable():
    schema = PatchSource.model_json_schema()
    assert "required" not in schema
    for name, prop in schema["properties"].items():
        assert prop.get("type") in ("string", "integer"), name
        assert "default" not in prop
    assert schema["properties"]["port"]["minimum"] == 1
    assert schema["properties"]["port"]["maximum"] == 65535
    assert schema["properties"]["password"]["writeOnly"] is True
    assert CreateSource.model_json_schema()["properties"]["password"]["writeOnly"] is True
