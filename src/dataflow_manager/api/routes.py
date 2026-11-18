"""CRUD HTTP boundary: fixed error projections and explicit envelope validation."""

import json
from email.message import Message
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from dataflow_manager.api.schemas import CreateSource, PatchSource, SourceRead
from dataflow_manager.services.sources import SourceService, ServiceError


_MESSAGES = {
    "malformed_json": (400, "Некорректный JSON"),
    "unsupported_media_type": (415, "Неподдерживаемый формат тела"),
    "validation_error": (422, "Некорректные параметры"),
    "route_not_found": (404, "Маршрут не найден"),
    "method_not_allowed": (405, "Метод не поддерживается"),
    "source_not_found": (404, "Источник не найден"),
    "source_name_conflict": (409, "Имя занято"),
    "operation_unavailable": (503, "Операция временно недоступна"),
    "internal_error": (500, "Внутренняя ошибка"),
}
_FIELDS = ("id", "name", "host", "port", "database", "username", "password", "request")
def _error_schema(validation=False):
    properties = {"code": {"type": "string"}, "message": {"type": "string"}}
    if validation:
        properties["fields"] = {"type": "array", "items": {"type": "string"}}
    return {"type": "object", "required": ["error"], "additionalProperties": False,
            "properties": {"error": {"type": "object", "additionalProperties": False,
                "required": ["code", "message", "fields"] if validation else ["code", "message"],
                "properties": properties}}}


_ERROR_RESPONSES = {status: {"description": description, "content": {"application/json": {"schema": _error_schema(status == 422)}}}
                    for status, description in ((400, "Некорректный JSON"), (415, "Неподдерживаемый формат тела"),
                                                (422, "Некорректные параметры"), (404, "Источник не найден"),
                                                (409, "Имя занято"), (503, "Операция временно недоступна"),
                                                (500, "Внутренняя ошибка"))}
router = APIRouter(prefix="/api/v1/sources", tags=["sources"], responses=_ERROR_RESPONSES)


class ApiProblem(Exception):
    def __init__(self, code, fields=()):
        self.code = code
        self.fields = tuple(field for field in _FIELDS if field in fields)
        super().__init__(code)


def problem_response(code, fields=(), headers=None):
    status, message = _MESSAGES[code]
    error = {"code": code, "message": message}
    if code == "validation_error":
        error["fields"] = [name for name in _FIELDS if name in fields]
    return JSONResponse({"error": error}, status_code=status, headers=headers)


def _model_fields(error):
    result = set()
    for issue in error.errors(include_input=False, include_context=False, include_url=False):
        location = issue.get("loc", ())
        first = location[0] if location else None
        result.add(first if first in _FIELDS[:-1] and issue.get("type") != "extra_forbidden" else "request")
    return result or {"request"}


def _reject_json_constant(_value):
    raise ValueError("invalid JSON constant")


async def _inputs(request, schema=None, source_id=None):
    body = await request.body()
    if schema is None:
        if body:
            raise ApiProblem("validation_error", ("request",))
        value = None
    else:
        if not body:
            raise ApiProblem("validation_error", ("request",))
        content_type = request.headers.get("content-type", "")
        mime = Message()
        mime["content-type"] = content_type
        params = mime.get_params() or []
        if (not params or params[0][0].lower() != "application/json"
                or len(params) > 2
                or (len(params) == 2 and (params[1][0].lower() != "charset"
                                          or not isinstance(params[1][1], str)
                                          or params[1][1].lower() != "utf-8"))):
            raise ApiProblem("unsupported_media_type")
        try:
            value = json.loads(body.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise ApiProblem("malformed_json") from None
    fields = set()
    identifier = None
    if source_id is not None:
        try:
            identifier = UUID(source_id)
            if identifier.version != 4 or str(identifier) != source_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            fields.add("id")
    if request.query_params:
        fields.add("request")
    if schema is not None:
        try:
            value = schema.model_validate(value)
        except ValidationError as error:
            fields.update(_model_fields(error))
    if fields:
        raise ApiProblem("validation_error", fields)
    return identifier, value


def _service(request):
    resources = getattr(request.app.state, "_dfm_db_resources", None)
    if resources is None:
        raise ApiProblem("operation_unavailable")
    return SourceService(resources[1], getattr(request.app.state, "_dfm_crypto", None))


async def _execute(request, method, *args):
    try:
        return await run_in_threadpool(getattr(_service(request), method), *args)
    except ServiceError as error:
        code = error.code if error.code in _MESSAGES else "internal_error"
        raise ApiProblem(code, error.fields) from None
    except ApiProblem:
        raise
    except Exception:
        raise ApiProblem("internal_error") from None


@router.post("", status_code=201, response_model=SourceRead,
             responses={201: {"description": "Источник создан", "headers": {"Location": {"schema": {"type": "string"}}}}},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": CreateSource.model_json_schema()}}}})
async def create_source(request: Request, response: Response):
    _, body = await _inputs(request, CreateSource)
    result = await _execute(request, "create", body.model_dump())
    response.headers["Location"] = f"/api/v1/sources/{result.id}"
    return result


@router.get("", response_model=list[SourceRead])
async def list_sources(request: Request):
    await _inputs(request)
    return await _execute(request, "list")


@router.get("/{source_id}", response_model=SourceRead)
async def get_source(source_id: str, request: Request):
    identifier, _ = await _inputs(request, source_id=source_id)
    return await _execute(request, "get", identifier)


@router.patch("/{source_id}", response_model=SourceRead,
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": PatchSource.model_json_schema()}}}})
async def update_source(source_id: str, request: Request):
    identifier, body = await _inputs(request, PatchSource, source_id)
    return await _execute(request, "update", identifier, body.model_dump(exclude_unset=True))


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, request: Request):
    identifier, _ = await _inputs(request, source_id=source_id)
    await _execute(request, "delete", identifier)
    return Response(status_code=204)
