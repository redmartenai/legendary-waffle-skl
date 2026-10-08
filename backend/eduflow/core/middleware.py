from __future__ import annotations

import time
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .logging import get_logger
from .request_context import (
    REQUEST_ID_HEADER,
    REQUEST_ID_META_KEY,
    bind_request_id,
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
