"""Validate a snapshot of the service database environment without I/O."""

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Mapping


class ConfigErrorCode(StrEnum):
    DB_CONFIG_MISSING = "DB_CONFIG_MISSING"
    DB_CONFIG_INVALID = "DB_CONFIG_INVALID"
    DB_ENV_FORBIDDEN = "DB_ENV_FORBIDDEN"


class ConfigError(ValueError):
    """Configuration failure containing only a fixed diagnostic code."""

    def __init__(self, code: ConfigErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    pool_size: int
    max_overflow: int
    pool_timeout: int
    connect_timeout: int
    pool_pre_ping: bool
    pool_recycle: int
    isolation_level: str


_FIELDS = (
    "HOST", "PORT", "NAME", "USER", "PASSWORD", "POOL_SIZE", "MAX_OVERFLOW",
    "POOL_TIMEOUT", "CONNECT_TIMEOUT", "POOL_PRE_PING", "POOL_RECYCLE",
    "ISOLATION_LEVEL",
)
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)", re.ASCII)
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}", re.ASCII)


def _integer(value: str, minimum: int, maximum: int) -> int:
    # Bound the length before conversion, including Python's integer digit limit.
    if not _INTEGER.fullmatch(value) or len(value) > len(str(maximum)):
        raise ConfigError(ConfigErrorCode.DB_CONFIG_INVALID)
    number = int(value)
    if not minimum <= number <= maximum:
        raise ConfigError(ConfigErrorCode.DB_CONFIG_INVALID)
    return number


def load_database_config(environment: Mapping[str, str]) -> DatabaseConfig:
    """Validate one caller-provided environment snapshot; never read os.environ."""
    values = dict(environment)
    if any(isinstance(key, str) and key.startswith("PG") for key in values):
        raise ConfigError(ConfigErrorCode.DB_ENV_FORBIDDEN)
    if any(f"DFM_DB_{name}" not in values for name in _FIELDS):
        raise ConfigError(ConfigErrorCode.DB_CONFIG_MISSING)
    config = {name: values[f"DFM_DB_{name}"] for name in _FIELDS}
    if not all(isinstance(value, str) for value in config.values()):
        raise ConfigError(ConfigErrorCode.DB_CONFIG_INVALID)

    if config["HOST"] != "127.0.0.1" or not all(
        _IDENTIFIER.fullmatch(config[name]) for name in ("NAME", "USER")
    ):
        raise ConfigError(ConfigErrorCode.DB_CONFIG_INVALID)

    password = config["PASSWORD"]
    password_encodable = True
    try:
        password.encode("utf-8", "strict")
    except UnicodeEncodeError:
        password_encodable = False
    if not password or "\x00" in password or not password_encodable:
        raise ConfigError(ConfigErrorCode.DB_CONFIG_INVALID)

    port = _integer(config["PORT"], 1, 65535)
    pool_size = _integer(config["POOL_SIZE"], 1, 20)
    max_overflow = _integer(config["MAX_OVERFLOW"], 0, 20)
    pool_timeout = _integer(config["POOL_TIMEOUT"], 1, 60)
    connect_timeout = _integer(config["CONNECT_TIMEOUT"], 1, 30)
    pool_recycle = _integer(config["POOL_RECYCLE"], 0, 86400)
    if pool_size + max_overflow > 20:
        raise ConfigError(ConfigErrorCode.DB_CONFIG_INVALID)
    pre_ping = config["POOL_PRE_PING"].lower()
    isolation = config["ISOLATION_LEVEL"].upper()
    if (not config["POOL_PRE_PING"].isascii() or not config["ISOLATION_LEVEL"].isascii()
            or pre_ping not in ("true", "false") or isolation != "READ COMMITTED"):
        raise ConfigError(ConfigErrorCode.DB_CONFIG_INVALID)

    return DatabaseConfig(
        host=config["HOST"], port=port, name=config["NAME"], user=config["USER"],
        password=password, pool_size=pool_size, max_overflow=max_overflow,
        pool_timeout=pool_timeout, connect_timeout=connect_timeout,
        pool_pre_ping=pre_ping == "true", pool_recycle=pool_recycle,
        isolation_level="READ COMMITTED",
    )
