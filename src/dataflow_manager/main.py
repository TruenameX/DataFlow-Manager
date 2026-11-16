from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from dataflow_manager.db.config import ConfigError, load_database_config
from dataflow_manager.db.session import ClientUnsupportedError, EngineInitError, _create_intermediate_resources


def _diagnose(code: str) -> None:
    try:
        logging.getLogger("dataflow_manager.lifecycle").error(code)
    except Exception:
        pass


def _initialize_database():
    try:
        config = load_database_config(os.environ)
        return _create_intermediate_resources(config)
    except ConfigError as error:
        # Only the closed configuration catalogue reaches ordinary logging.
        _diagnose(error.code.value)
    except ClientUnsupportedError:
        _diagnose("DB_CLIENT_UNSUPPORTED")
    except EngineInitError:
        _diagnose("DB_ENGINE_INIT_FAILED")
    except Exception:
        _diagnose("DB_ENGINE_INIT_FAILED")
    return None


@asynccontextmanager
async def lifespan(application: FastAPI):
    # None means unavailable, not a database readiness assertion.
    application.state._dfm_db_resources = None
    resources = await run_in_threadpool(_initialize_database)
    try:
        application.state._dfm_db_resources = resources
        yield
    finally:
        application.state._dfm_db_resources = None
        if resources is not None:
            try:
                await run_in_threadpool(resources[0].dispose)
            except Exception:
                _diagnose("DB_DISPOSE_FAILED")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
