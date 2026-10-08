"""Django-level error views, so even errors outside DRF (unknown URLs, CSRF, crashes) use the envelope."""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse

from .exceptions import error_response


def bad_request(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return error_response(400, "bad_request", "The request could not be understood.")


def permission_denied(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return error_response(403, "permission_denied", "You don't have permission to do that.")


def not_found(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return error_response(404, "not_found", "We couldn't find that.")


def server_error(request: HttpRequest) -> JsonResponse:
    return error_response(500, "server_error", "The server had a problem. Please try again.")


def csrf_failure(request: HttpRequest, reason: str = "") -> JsonResponse:
    return error_response(403, "csrf_failed", "The request was rejected. Please reload and try again.")
