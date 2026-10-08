from __future__ import annotations

import time
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from . import db_context
from .client_ip import client_ip, user_agent
from .logging import get_logger
from .request_context import (
    REQUEST_ID_HEADER,
    REQUEST_ID_META_KEY,
    bind_request_id,
    bind_request_info,
    clear_context,
    normalize_request_id,
)

access_log = get_logger("eduflow.access")


class RequestContextMiddleware:
    """Assign a request ID, bind it to the logging context, echo it, and write one access-log line.

    This runs first, so every later log line in the request (including errors) carries the ID.
    The access log records the path but **not** the query string, which may carry personal data.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        clear_context()
        request_id = normalize_request_id(request.META.get(REQUEST_ID_META_KEY))
        request.request_id = request_id  # type: ignore[attr-defined]
        bind_request_id(request_id)
        bind_request_info(ip=client_ip(request), user_agent=user_agent(request))

        started = time.perf_counter()
        response: HttpResponse | None = None
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            status = response.status_code if response is not None else 500
            user = getattr(request, "user", None)
            user_id = getattr(user, "pk", None) if getattr(user, "is_authenticated", False) else None
            log = access_log.warning if status >= 500 else access_log.info
            log(
                "http_request",
                method=request.method,
                path=request.path,
                status=status,
                duration_ms=duration_ms,
                user_id=str(user_id) if user_id is not None else None,
            )
            clear_context()


# The liveness probe must do no I/O, and the readiness probe checks the database as the login role.
_DB_CONTEXT_EXEMPT = ("/api/v1/health/",)


class DatabaseContextMiddleware:
    """Run every request under the RLS-enforced application role, with an empty tenant context.

    Authentication then adds the user, and tenant resolution adds the school (``eduflow.core.db_context``).
    The context is always released afterwards, so a persistent connection carries nothing into the next
    request.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith(_DB_CONTEXT_EXEMPT):
            return self.get_response(request)
        db_context.engage()
        try:
            return self.get_response(request)
        finally:
            db_context.release()


# A JSON API needs no scripts, styles, frames or forms. Swagger UI (development only) is excluded.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class SecurityHeadersMiddleware:
    """Headers Django's SecurityMiddleware does not set: CSP, and no caching of API responses.

    API responses can carry tokens and personal data, so no browser or intermediary may store them.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        docs = request.path.startswith("/api/v1/docs")
        if not docs:
            response.setdefault("Content-Security-Policy", _API_CSP)
        if request.path.startswith("/api/") and not docs:
            response.setdefault("Cache-Control", "no-store")
        response.setdefault(
            "Cross-Origin-Resource-Policy", getattr(settings, "CROSS_ORIGIN_RESOURCE_POLICY", "same-origin")
        )
        return response
