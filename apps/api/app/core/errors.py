"""Structured error responses. Never leak stack traces over the wire."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Application-level error with a stable code, message, and HTTP status."""

    code: str = "internal_error"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthError(AppError):
    code = "unauthorized"
    http_status = status.HTTP_401_UNAUTHORIZED


class ForbiddenError(AppError):
    code = "forbidden"
    http_status = status.HTTP_403_FORBIDDEN


class NotFoundError(AppError):
    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND


class ValidationError(AppError):
    code = "validation_error"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY


class PanGstinReconciliationError(AppError):
    code = "pan_gstin_mismatch"
    http_status = status.HTTP_409_CONFLICT


class RateLimitedError(AppError):
    """Upstream AI provider is throttling us. Surfaced as HTTP 429 with a
    ``Retry-After`` header so the UI can count down and retry."""

    code = "rate_limited"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            details={**(details or {}), "retry_after_seconds": retry_after_seconds},
        )
        self.retry_after_seconds = retry_after_seconds


class AIProviderError(AppError):
    """Upstream AI provider failed (bad output, outage). Not the caller's
    fault, so 502 rather than 422 / 500."""

    code = "ai_provider_error"
    http_status = status.HTTP_502_BAD_GATEWAY


# Default wait we advertise when the provider didn't say how long to back off.
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 30


def llm_error_to_app_error(exc: Exception, *, action: str) -> AppError:
    """Translate an LLM-layer exception into a partner-friendly AppError.

    ``action`` is the user-facing noun ("triage", "draft"). Raw provider
    strings ("openai call failed: Error code: 429 ...") are logged by the
    caller but never put on the wire.
    """
    from app.services.llm.base import LLMRateLimitError

    cause: BaseException | None = exc
    while cause is not None and not isinstance(cause, LLMRateLimitError):
        cause = cause.__cause__
    if isinstance(cause, LLMRateLimitError):
        hinted = cause.retry_after_seconds
        wait = (
            int(min(max(hinted, 5), 3600))
            if hinted is not None
            else DEFAULT_RATE_LIMIT_RETRY_SECONDS
        )
        return RateLimitedError(
            f"The AI provider is busy (rate limit or temporary overload) while "
            f"generating the {action}. "
            f"Please wait about {wait} seconds and try again.",
            retry_after_seconds=wait,
        )
    return AIProviderError(
        f"The AI provider could not generate the {action} right now. "
        "Please try again in a moment; if this keeps happening, check the "
        "API server logs."
    )


DB_UNAVAILABLE_MESSAGE = (
    "The database is not reachable, so the request could not be completed. "
    "Check that the PostgreSQL service is running, then try again."
)


def is_db_unavailable(exc: BaseException) -> bool:
    """True when ``exc`` (or anything in its cause chain) means the API could
    not connect to Postgres at all — service stopped, wrong host/port.

    asyncpg raises a plain ``OSError("Multiple exceptions: [Errno 10061]
    Connect call failed ...")`` when nothing listens on the port; SQLAlchemy
    does not wrap it, so it used to fall through to the catch-all handler
    and reach the UI as a bare "internal server error".
    """
    from sqlalchemy.exc import InterfaceError, OperationalError

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ConnectionError):
            return True
        msg = str(cur).lower()
        if isinstance(cur, OSError) and "connect call failed" in msg:
            return True
        if isinstance(cur, (OperationalError, InterfaceError)) and (
            "connect" in msg or "connection refused" in msg
        ):
            return True
        if type(cur).__module__.startswith("asyncpg") and type(cur).__name__ in (
            "CannotConnectNowError",
            "ConnectionDoesNotExistError",
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _db_unavailable_response(exc: BaseException) -> JSONResponse:
    logger.error("database_unavailable", error=str(exc))
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=_envelope("database_unavailable", DB_UNAVAILABLE_MESSAGE),
        headers={"Retry-After": "30"},
    )


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        logger.info("app_error", code=exc.code, message=exc.message, details=exc.details)
        headers: dict[str, str] | None = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after_seconds)}
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(
                "validation_error",
                "request validation failed",
                {"errors": exc.errors()},
            ),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # Surface the underlying message in development so contributors can
        # see "relation documents_inbox does not exist" directly. In
        # production we still return an opaque message — DB errors can
        # contain row data we don't want on the wire.
        from app.core.config import get_settings

        if is_db_unavailable(exc):
            return _db_unavailable_response(exc)
        details: dict[str, Any] = {}
        if get_settings().environment == "development":
            details = {"error": str(exc)}
        logger.error("db_error", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("database_error", "database error", details),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        from app.core.config import get_settings

        if is_db_unavailable(exc):
            return _db_unavailable_response(exc)
        details: dict[str, Any] = {}
        if get_settings().environment == "development":
            details = {"error": str(exc), "type": type(exc).__name__}
        logger.error("unhandled_exception", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal_error", "internal server error", details),
        )
