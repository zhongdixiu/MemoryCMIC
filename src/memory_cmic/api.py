from __future__ import annotations

import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.api_schemas import AddRequest, ErrorBody, ErrorInfo, TaskResponse
from memory_cmic.auth import (
    AuthContext,
    AuthenticationError,
    AuthorizationError,
)
from memory_cmic.db import create_database_engine
from memory_cmic.ingest import IngestError, ingest_and_enqueue
from memory_cmic.models import MemoryTask
from memory_cmic.settings import Settings
from memory_cmic.task_results import to_task_response


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_response(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    body = ErrorBody(error=ErrorInfo(code=code, message=message))
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


def _load_task(
    session_factory: sessionmaker[Session], auth: AuthContext, task_id: str
) -> MemoryTask | None:
    with session_factory() as session:
        return session.scalars(
            select(MemoryTask).where(
                MemoryTask.id == task_id,
                MemoryTask.tenant_id == auth.tenant_id,
                MemoryTask.caller_agent_id == auth.caller_agent_id,
            )
        ).one_or_none()


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_engine = engine or create_database_engine(resolved_settings.database_url)
    session_factory = sessionmaker(resolved_engine, expire_on_commit=False)
    app = FastAPI(title="MemoryCMIC", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.engine = resolved_engine
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
        if len(request_id) > 128:
            request_id = f"req_{uuid4().hex}"
        request.state.request_id = request_id
        if request.method == "POST" and len(await request.body()) > 2 * 1024 * 1024:
            return _error_response(
                413, "PAYLOAD_TOO_LARGE", "request body exceeds 2 MiB", request_id
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return _error_response(
            exc.status_code, exc.code, exc.message, request.state.request_id
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {"msg": "invalid request"}
        if first.get("type") == "json_invalid":
            return _error_response(
                400, "INVALID_JSON", "request body is not valid JSON", request.state.request_id
            )
        return _error_response(
            422,
            "INVALID_MESSAGE",
            str(first.get("msg", "invalid request")),
            request.state.request_id,
        )

    def authenticate(authorization: str | None = Header(default=None)) -> AuthContext:
        try:
            return resolved_settings.credentials.authenticate(authorization)
        except AuthenticationError as exc:
            raise ApiError(401, "UNAUTHENTICATED", str(exc)) from exc

    @app.post(
        "/api/v1/memories:add",
        response_model=TaskResponse,
        responses={400: {"model": ErrorBody}, 409: {"model": ErrorBody}},
    )
    def add_memories(
        body: AddRequest,
        request: Request,
        response: Response,
        auth: AuthContext = Depends(authenticate),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TaskResponse:
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or not idempotency_key.isascii()
        ):
            raise ApiError(
                422,
                "INVALID_MESSAGE",
                "Idempotency-Key must contain 1-128 ASCII characters",
            )
        try:
            resolved_settings.credentials.authorize_source(auth, body.source_system)
        except AuthorizationError as exc:
            raise ApiError(403, "FORBIDDEN", str(exc)) from exc

        try:
            with session_factory.begin() as session:
                receipt = ingest_and_enqueue(
                    session,
                    auth=auth,
                    request=body,
                    idempotency_key=idempotency_key,
                    request_id=request.state.request_id,
                )
        except IngestError as exc:
            status_code = 409 if exc.code.endswith("CONFLICT") else 422
            raise ApiError(status_code, exc.code, str(exc)) from exc

        task = receipt.task
        if body.wait_for_result and task.status in {"pending", "processing"}:
            deadline = time.monotonic() + (body.wait_timeout_ms or 10_000) / 1000
            while time.monotonic() < deadline:
                time.sleep(0.1)
                refreshed = _load_task(session_factory, auth, task.id)
                if refreshed is not None:
                    task = refreshed
                if task.status not in {"pending", "processing"}:
                    break

        task_response = to_task_response(task)
        response.status_code = 202 if task_response.status == "pending" else 200
        return task_response

    @app.get(
        "/api/v1/tasks/{task_id}",
        response_model=TaskResponse,
        responses={404: {"model": ErrorBody}},
    )
    def get_task(task_id: str, auth: AuthContext = Depends(authenticate)) -> TaskResponse:
        if not 1 <= len(task_id) <= 128:
            raise ApiError(404, "TASK_NOT_FOUND", "task does not exist")
        task = _load_task(session_factory, auth, task_id)
        if task is None:
            raise ApiError(404, "TASK_NOT_FOUND", "task does not exist")
        return to_task_response(task)

    return app
