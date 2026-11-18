from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import ResponseValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool

from dataflow_manager.db.config import ConfigError, load_database_config
from dataflow_manager.db.session import ClientUnsupportedError, EngineInitError, _create_intermediate_resources
from dataflow_manager.security.crypto import CryptoError, CryptoService
from dataflow_manager.api.routes import ApiProblem, problem_response, router
from dataflow_manager.api.access_log import acquire_access_filter, release_access_filter


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
    acquire_access_filter()
    application.state._dfm_db_resources = None
    application.state._dfm_crypto = None
    resources = None
    try:
        try:
            application.state._dfm_crypto = CryptoService.from_environment(dict(os.environ))
        except CryptoError:
            pass
        resources = await run_in_threadpool(_initialize_database)
        application.state._dfm_db_resources = resources
        yield
    finally:
        application.state._dfm_db_resources = None
        application.state._dfm_crypto = None
        try:
            if resources is not None:
                await run_in_threadpool(resources[0].dispose)
        except Exception:
            _diagnose("DB_DISPOSE_FAILED")
        finally:
            release_access_filter()


app = FastAPI(lifespan=lifespan)
app.include_router(router)


@app.exception_handler(ApiProblem)
async def api_problem_handler(_request: Request, error: ApiProblem):
    return problem_response(error.code, error.fields)


@app.exception_handler(ResponseValidationError)
async def response_validation_handler(_request: Request, _error: ResponseValidationError):
    return problem_response("internal_error")


@app.exception_handler(StarletteHTTPException)
async def http_problem_handler(_request: Request, error: StarletteHTTPException):
    if error.status_code == 404:
        return problem_response("route_not_found")
    if error.status_code == 405:
        headers = {"Allow": error.headers["Allow"]} if error.headers and "Allow" in error.headers else None
        return problem_response("method_not_allowed", headers=headers)
    return problem_response("internal_error")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
