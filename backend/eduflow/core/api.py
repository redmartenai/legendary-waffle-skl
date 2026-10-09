"""Shared API building blocks: strict serializers, typed errors and their OpenAPI descriptions."""

from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import APIException


class StrictSerializer(serializers.Serializer[Any]):
    """Rejects unknown fields, so a client cannot slip extra attributes into a write (mass assignment)."""

    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({name: ["Unknown field."] for name in unknown})
        return super().to_internal_value(data)


# --------------------------------------------------------------------------- errors
# Each class is its own stable ``code`` in the error envelope (eduflow.core.exceptions).


class TenantRequired(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "tenant_required"
    default_detail = "Choose a school first."


class TenantForbidden(APIException):
    """The caller is not an active member of an active school with that ID. It never says which."""

    status_code = status.HTTP_403_FORBIDDEN
    default_code = "tenant_forbidden"
    default_detail = "You don't have access to this school."


class InvalidCredentials(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = "invalid_credentials"
    default_detail = "The details you entered are incorrect."


class InvalidOtp(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = "invalid_code"
    default_detail = "That code is incorrect or has expired."


class InvalidRefreshToken(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = "not_authenticated"
    default_detail = "Please sign in again."


class InvalidInvitation(APIException):
    """Unknown, expired, revoked or already accepted: recipients cannot tell which."""

    status_code = status.HTTP_404_NOT_FOUND
    default_code = "invitation_invalid"
    default_detail = "This invitation is no longer valid. Ask the school to send a new one."


class AccountExists(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "account_exists"
    default_detail = (
        "An account already uses this email address or mobile number. "
        "Sign in to it, then accept the invitation."
    )


class PasswordChangeRequired(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "password_change_required"
    default_detail = "Please set a new password to continue."


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "That conflicts with the current state."


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "service_unavailable"
    default_detail = "This service is temporarily unavailable."


# --------------------------------------------------------------------------- OpenAPI
ErrorEnvelope = inline_serializer(
    "ErrorEnvelope",
    fields={
        "error": inline_serializer(
            "ErrorBody",
            fields={
                "code": serializers.CharField(),
                "message": serializers.CharField(),
                "fields": serializers.DictField(required=False),
                "retry_after_seconds": serializers.IntegerField(required=False),
                "request_id": serializers.CharField(required=False),
            },
        )
    },
)

_ERROR_DESCRIPTIONS = {
    400: "`validation_error` (details in `fields`), `parse_error` or `tenant_required`",
    401: "`not_authenticated`: missing, invalid, expired or revoked credentials",
    403: "`permission_denied`, `tenant_forbidden` or `password_change_required`",
    404: "`not_found`: does not exist, or is outside the caller's school or data scope (indistinguishable); "
    "`invitation_invalid` on invitation recipient endpoints",
    409: "`conflict`, or `account_exists` when accepting an invitation",
    429: "`rate_limited`: see `retry_after_seconds` and the `Retry-After` header",
    503: "`service_unavailable`",
}


def errors(*codes: int) -> dict[int, OpenApiResponse]:
    return {c: OpenApiResponse(ErrorEnvelope, description=_ERROR_DESCRIPTIONS[c]) for c in codes}


TENANT_HEADER = OpenApiParameter(
    "X-School-Id",
    OpenApiTypes.UUID,
    location=OpenApiParameter.HEADER,
    required=True,
    description=(
        "The school this request acts in. It is checked against the caller's active memberships on every "
        "request; a school the caller does not belong to gives `403 tenant_forbidden`."
    ),
)
