import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .tenant import TenantContextMissing

logger = logging.getLogger("apps.core")


def api_exception_handler(exc, context):
    """Return every error as {"error": {"code", "message", "fields"?}}."""
    if isinstance(exc, DjangoValidationError):
        return Response(
            {"error": {"code": "invalid", "message": "; ".join(exc.messages)}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, TenantContextMissing):
        # A programming error: never leak data, never guess a school.
        logger.error("Tenant context missing: %s", exc)
        return Response(
            {"error": {"code": "tenant_context", "message": "Something went wrong."}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    response = exception_handler(exc, context)
    if response is None:
        return None

    data = response.data
    code = getattr(exc, "default_code", "error")
    if isinstance(data, dict) and set(data.keys()) <= {"detail"}:
        body = {"code": code, "message": str(data.get("detail", ""))}
    else:
        body = {"code": code, "message": "Please check the highlighted fields.", "fields": data}
    wait = getattr(exc, "wait", None)
    if wait:
        body["retry_after_seconds"] = int(wait)
    response.data = {"error": body}
    return response
