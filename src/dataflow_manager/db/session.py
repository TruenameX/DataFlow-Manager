"""Offline libpq checks and an Engine with a guarded connection path."""

from dataclasses import dataclass
import logging
import os
import threading

from dataflow_manager.db.config import ConfigError, ConfigErrorCode, DatabaseConfig


class ClientUnsupportedError(RuntimeError):
    """A fixed diagnostic for an unavailable or unsupported libpq client."""

    code = "DB_CLIENT_UNSUPPORTED"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class LibpqCapability:
    implementation: str
    version: int


def _load_pq():
    # Delay driver loading until the caller explicitly requests the check.
    from psycopg import pq

    return pq


def check_libpq_capability() -> LibpqCapability:
    """Check client metadata without connecting or accepting credentials.

    Recognizing require_auth is not evidence of a successful SCRAM exchange.
    The caller must apply the startup environment policy before this check.
    """
    supported = False
    try:
        pq = _load_pq()
        implementation = pq.__impl__
        version = pq.version()
        if implementation in ("python", "c", "binary") and type(version) is int and version > 0:
            options = pq.Conninfo.parse(b"require_auth=scram-sha-256")
            supported = any(option.keyword == b"require_auth" for option in options)
    except Exception:
        # Do not attach or render the raw client exception at the public boundary.
        supported = False

    if not supported:
        raise ClientUnsupportedError()
    return LibpqCapability(implementation=implementation, version=version)


class EngineInitError(RuntimeError):
    code = "DB_ENGINE_INIT_FAILED"

    def __init__(self) -> None:
        super().__init__(self.code)


_POOL_LOGGER_NAME = "sqlalchemy.pool.impl.QueuePool.dataflow_manager_service"
_POOL_LOGGING_NAME = "dataflow_manager_service"
_pool_logger_lock = threading.Lock()
_pool_handler = None


class _SafePoolHandler(logging.Handler):
    """Forward only fixed diagnostics, never format or forward pool records."""

    def handle(self, record):
        # No handler lock: forwarding must not invert logging configuration locks.
        try:
            if record.levelno >= logging.ERROR:
                level, category, message = logging.ERROR, "POOL_DIAGNOSTIC_ERROR", "Pool diagnostic error"
            else:
                level, category, message = logging.INFO, "POOL_DIAGNOSTIC_EVENT", "Pool diagnostic event"
            logging.getLogger("dataflow_manager.db.pool_diagnostics").log(
                level, message, extra={"category": category}
            )
        except Exception:
            # Diagnostics must not replace the exception being handled by SQLAlchemy.
            pass
        return True


def _configure_pool_diagnostics():
    global _pool_handler
    with _pool_logger_lock:
        logger = logging.getLogger(_POOL_LOGGER_NAME)
        if _pool_handler is None:
            if logger.handlers or logger.filters or logger.level != logging.NOTSET or not logger.propagate or logger.disabled:
                raise RuntimeError("pool diagnostic channel conflict")
            handler = _SafePoolHandler()
            try:
                logger.propagate = False
                logger.setLevel(logging.DEBUG)
                logger.addHandler(handler)
                _pool_handler = handler
            except Exception:
                if handler in logger.handlers:
                    logger.removeHandler(handler)
                logger.setLevel(logging.NOTSET)
                logger.propagate = True
                raise
        elif (logger.handlers != [_pool_handler] or logger.filters or logger.level != logging.DEBUG
              or logger.propagate or logger.disabled or _pool_handler.filters
              or _pool_handler.level != logging.NOTSET):
            raise RuntimeError("pool diagnostic channel conflict")
        return logger


def _verify_engine_pool(engine, logger):
    from sqlalchemy.pool import QueuePool

    if type(engine.pool) is not QueuePool or engine.pool.logger is not logger:
        raise RuntimeError("unexpected pool diagnostic channel")


def _check_environment():
    if any(name.startswith("PG") for name in os.environ):
        raise ConfigError(ConfigErrorCode.DB_ENV_FORBIDDEN)


def _guard_connect(dialect, connection_record, cargs, cparams):
    """Reject forbidden libpq environment before each new DBAPI connection."""
    _check_environment()


def _create_intermediate_resources(config: DatabaseConfig):
    """Build guarded resources; not for application use before pool acceptance."""
    _check_environment()
    check_libpq_capability()

    engine = None
    pool_verified = False
    failed = False
    dispose_failed = False
    cleanup_skipped = False
    try:
        from sqlalchemy import create_engine, event
        from sqlalchemy.engine import URL
        from sqlalchemy.orm import sessionmaker

        url = URL.create(
            "postgresql+psycopg", username=config.user, password=config.password,
            host=config.host, port=config.port, database=config.name,
        )
        pool_logger = _configure_pool_diagnostics()
        engine = create_engine(
            url,
            connect_args={
                "connect_timeout": config.connect_timeout,
                "sslmode": "disable",
                "gssencmode": "disable",
                "require_auth": "scram-sha-256",
            },
            pool_size=config.pool_size, max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout, pool_pre_ping=config.pool_pre_ping,
            pool_recycle=config.pool_recycle, isolation_level=config.isolation_level,
            echo=False, hide_parameters=True, echo_pool=False,
            pool_logging_name=_POOL_LOGGING_NAME,
        )
        _verify_engine_pool(engine, pool_logger)
        pool_verified = True
        event.listen(engine, "do_connect", _guard_connect)
        factory = sessionmaker(bind=engine, autobegin=False, close_resets_only=False)
    except Exception:
        failed = True
        if engine is not None:
            if not pool_verified:
                # A foreign pool could log raw errors during dispose. No checkout
                # has occurred, so do not invoke its unverified diagnostic path.
                cleanup_skipped = True
            else:
                try:
                    engine.dispose()
                except Exception:
                    dispose_failed = True
    if failed:
        if dispose_failed or cleanup_skipped:
            try:
                logging.getLogger(__name__).error(
                    "DB_DISPOSE_FAILED" if dispose_failed else "POOL_CLEANUP_SKIPPED_UNVERIFIED"
                )
            except Exception:
                # A diagnostic handler must not replace the primary failure.
                pass
        # Raise outside the handler so raw exception context is not exposed.
        raise EngineInitError()
    return engine, factory
