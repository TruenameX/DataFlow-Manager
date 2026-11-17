import logging

import pytest
from cryptography.fernet import Fernet

from dataflow_manager.security.crypto import CryptoError, CryptoErrorCode, CryptoService


@pytest.mark.parametrize("password", ["a", "пароль🔒", " leading and trailing ", "a\x00b"])
def test_round_trip_and_randomized_ciphertext(password):
    service = CryptoService(Fernet.generate_key().decode("ascii"))
    first = service.encrypt(password)
    second = service.encrypt(password)
    assert type(first) is str and first != password and first != second
    if len(password) >= 10:
        assert password.encode("utf-8") not in first.encode("ascii")
    assert service.decrypt(first) == password
    assert service.decrypt(second) == password


@pytest.mark.parametrize("key, code", [
    (None, CryptoErrorCode.KEY_MISSING), ("", CryptoErrorCode.KEY_MISSING),
    ("synthetic-invalid-key", CryptoErrorCode.KEY_INVALID),
    ("🔒", CryptoErrorCode.KEY_INVALID), (object(), CryptoErrorCode.KEY_INVALID),
])
def test_invalid_key_is_safe_and_never_generates_key(key, code, monkeypatch, caplog):
    def forbidden():
        raise AssertionError("unexpected key generation")
    monkeypatch.setattr(Fernet, "generate_key", forbidden)
    with pytest.raises(CryptoError) as caught:
        CryptoService(key)
    assert caught.value.code is code
    assert "synthetic-invalid-key" not in str(caught.value) + repr(caught.value) + caplog.text


def test_explicit_configuration_snapshot_and_no_key_fallback(monkeypatch):
    monkeypatch.setenv("DATAFLOW_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(CryptoError) as caught:
        CryptoService.from_environment({})
    assert caught.value.code is CryptoErrorCode.KEY_MISSING
    supplied_key = Fernet.generate_key().decode("ascii")
    service = CryptoService.from_environment({"DATAFLOW_FERNET_KEY": supplied_key})
    assert service.decrypt(service.encrypt("secret")) == "secret"


def test_noncanonical_key_encodings_are_rejected():
    key = Fernet.generate_key().decode("ascii")
    for invalid in (key + "!", key + "\n", " " + key, key[:-1], key.replace("=", "==")):
        with pytest.raises(CryptoError) as caught:
            CryptoService(invalid)
        assert caught.value.code is CryptoErrorCode.KEY_INVALID


def test_corruption_and_unknown_key_have_same_safe_failure(caplog):
    first = CryptoService(Fernet.generate_key().decode("ascii"))
    other = CryptoService(Fernet.generate_key().decode("ascii"))
    token = first.encrypt("synthetic-plaintext-secret")
    for bad_token in (token, "synthetic-invalid-token", "🔒", object()):
        service = other if bad_token == token else first
        with pytest.raises(CryptoError) as caught:
            service.decrypt(bad_token)
        assert caught.value.code is CryptoErrorCode.DECRYPTION_FAILED
        assert "synthetic" not in str(caught.value) + repr(caught.value)
        assert token not in str(caught.value) + repr(caught.value)
    assert "synthetic-plaintext-secret" not in caplog.text


def test_failure_diagnostics_and_positive_control(caplog):
    marker = "synthetic-credential-marker"
    caplog.set_level(logging.WARNING)
    logging.getLogger("positive-control").warning("%s", marker)
    assert marker in caplog.text
    caplog.clear()
    service = CryptoService(Fernet.generate_key().decode("ascii"))
    with pytest.raises(CryptoError) as caught:
        service.encrypt("\ud800" + marker)
    assert caught.value.code is CryptoErrorCode.ENCRYPTION_FAILED
    assert marker not in str(caught.value) + repr(caught.value) + repr(caught.value.args) + caplog.text


def test_base_exception_is_not_suppressed():
    service = CryptoService(Fernet.generate_key().decode("ascii"))
    interruption = KeyboardInterrupt()
    class FakeFernet:
        def encrypt(self, _value):
            raise interruption
    service._fernet = FakeFernet()
    with pytest.raises(KeyboardInterrupt) as caught:
        service.encrypt("secret")
    assert caught.value is interruption
