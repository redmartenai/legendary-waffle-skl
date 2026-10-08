"""The single error envelope every API error uses (ADR-006).

    {"error": {"code": "<stable machine code>", "message": "<safe human text>",
               "fields": {...}?, "retry_after_seconds": n?, "request_id": "..."}}

The client (``src/api/client.ts``) reads ``code``, ``message``, ``fields`` and ``retry_after_seconds``.
``request_id`` lets support trace an error to its log lines. Internal details (exception text, SQL,
stack traces) never reach the response.
"""

from __future__ import annotations

import math
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404, JsonResponse
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import set_rollback

from .logging import get_logger
from .request_context import get_request_id

log = get_logger(__name__)

# DRF exception class -> (code, default message). Order matters: most specific first.
_CODES: list[tuple[type[exceptions.APIException], str, str]] = [
    (exceptions.ValidationError, "validation_error", "Some fields need attention."),
    (exceptions.ParseError, "parse_error", "The request body could not be read."),
    (exceptions.AuthenticationFailed, "not_authenticated", "Please sign in again."),
    (exceptions.NotAuthenticated, "not_authenticated", "Please sign in."),
    (exceptions.PermissionDenied, "permission_denied", "You don't have permission to do that."),
    (exceptions.NotFound, "not_found", "We couldn't find that."),
    (exceptions.MethodNotAllowed, "method_not_allowed", "That action isn't supported here."),
    (exceptions.NotAcceptable, "not_acceptable", "That response format isn't supported."),
    (exceptions.UnsupportedMediaType, "unsupported_media_type", "That content type isn't supported."),
    (exceptions.Throttled, "rate_limited", "Too many requests. Please wait and try again."),
]


def error_body(
    code: str,
    message: str,
    *,
    fields: dict[str, Any] | None = None,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    if retry_after_seconds is not None:
        error["retry_after_seconds"] = retry_after_seconds
    request_id = get_request_id()
    if request_id:
        error["request_id"] = request_id
    return {"error": error}


def error_response(status_code: int, code: str, message: str) -> JsonResponse:
    """Envelope for errors raised outside DRF (Django's 400/403/404/500 handlers)."""
    return JsonResponse(error_body(code, message), status=status_code)


def _validation_fields(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, list):
        return {"non_field_errors": detail}
    return {"non_field_errors": [detail]}


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    # Translate the Django exceptions DRF also handles.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    if isinstance(exc, exceptions.APIException):
        code, message = "error", "Something went wrong."
        for cls, mapped_code, default_message in _CODES:
            if isinstance(exc, cls):
                code, message = mapped_code, default_message
                break

        fields = None
        retry_after = None
        if isinstance(exc, exceptions.ValidationError):
            fields = _validation_fields(exc.detail)
        elif isinstance(exc, exceptions.Throttled) and (wait := getattr(exc, "wait", None)) is not None:
            retry_after = max(1, math.ceil(wait))
        elif not isinstance(exc, tuple(c for c, _, _ in _CODES)) and isinstance(exc.detail, str):
            # A custom APIException subclass: its own (developer-written) message and code are safe.
            message = str(exc.detail)
            code = str(exc.default_code)

        headers: dict[str, str] = {}
        if getattr(exc, "auth_header", None):
            headers["WWW-Authenticate"] = exc.auth_header  # type: ignore[attr-defined]
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)

        set_rollback()
        return Response(
            error_body(code, message, fields=fields, retry_after_seconds=retry_after),
            status=exc.status_code,
            headers=headers,
        )

    # Anything else is a bug. Log it with the traceback and return a generic 500 with no details.
    view = context.get("view")
    log.exception("unhandled_api_exception", view=type(view).__name__ if view else None)
    set_rollback()
    return Response(
        error_body("server_error", "The server had a problem. Please try again."),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
