"""Explicit, offline Fernet encryption for saved source credentials.

The caller supplies the configuration snapshot; this module never reads the
environment, generates keys, accesses a database, or logs sensitive material.
"""

from collections.abc import Mapping
import base64
from enum import StrEnum

from cryptography.fernet import Fernet


class CryptoErrorCode(StrEnum):
    KEY_MISSING = "KEY_MISSING"
    KEY_INVALID = "KEY_INVALID"
    ENCRYPTION_FAILED = "ENCRYPTION_FAILED"
    DECRYPTION_FAILED = "DECRYPTION_FAILED"


class CryptoError(RuntimeError):
    """Fixed diagnostic; traceback and exception context are not sanitized."""

    def __init__(self, code: CryptoErrorCode) -> None:
        if type(code) is not CryptoErrorCode:
            raise ValueError("invalid crypto error")
        self.code = code
        super().__init__(code.value)


class CryptoService:
    """Encrypt and decrypt UTF-8 passwords using one explicitly supplied key."""

    def __init__(self, key: str | None) -> None:
        if key is None or (type(key) is str and not key):
            raise CryptoError(CryptoErrorCode.KEY_MISSING)
        try:
            if type(key) is not str:
                raise TypeError
            raw = key.encode("ascii")
            decoded = base64.b64decode(raw, altchars=b"-_", validate=True)
            if len(decoded) != 32 or base64.urlsafe_b64encode(decoded) != raw:
                raise ValueError
            self._fernet = Fernet(raw)
        except Exception:
            raise CryptoError(CryptoErrorCode.KEY_INVALID) from None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "CryptoService":
        """Initialize from a caller-provided snapshot; never read os.environ."""
        try:
            key = environment.get("DATAFLOW_FERNET_KEY")
        except Exception:
            raise CryptoError(CryptoErrorCode.KEY_INVALID) from None
        return cls(key)

    def encrypt(self, password: str) -> str:
        try:
            if type(password) is not str:
                raise TypeError
            return self._fernet.encrypt(password.encode("utf-8", "strict")).decode("ascii")
        except Exception:
            raise CryptoError(CryptoErrorCode.ENCRYPTION_FAILED) from None

    def decrypt(self, token: str) -> str:
        try:
            if type(token) is not str:
                raise TypeError
            return self._fernet.decrypt(token.encode("ascii", "strict")).decode("utf-8", "strict")
        except Exception:
            # InvalidToken does not distinguish corruption from an unknown key.
            raise CryptoError(CryptoErrorCode.DECRYPTION_FAILED) from None
