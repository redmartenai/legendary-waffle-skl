"""Authentication and account endpoints (``/api/v1/auth/*``, ``/api/v1/me``).

Sign-in endpoints are public, rate-limited and give one generic error for every failure. Responses carrying
tokens are ``Cache-Control: no-store`` (SecurityHeadersMiddleware).
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from eduflow.authz.api.base import AuthenticatedAPIView
from eduflow.core import db_context
from eduflow.core.api import errors
from eduflow.tenancy.selectors import memberships_for_user

from .. import services, throttles, tokens
from ..authentication import request_user
from ..models import AuthSession, RevokeReason
from ..otp import service as otp
from . import serializers as s

AUTH_TAG = ["auth"]


def _session_payload(issued: tokens.IssuedTokens) -> dict[str, Any]:
    user = issued.session.user
    if db_context.current() is not None:
        db_context.update(user_id=user.pk)  # so RLS lets the new user read their own memberships
    return {
        "access": issued.access,
        "refresh": issued.refresh,
        "token_type": "Bearer",
        "access_expires_in": issued.access_expires_in,
        "refresh_expires_at": issued.refresh_expires_at,
        "user": s.UserOut(user).data,
        "memberships": s.MyMembershipOut(memberships_for_user(user), many=True).data,
    }


class PublicAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []


class PasswordLoginView(PublicAPIView):
    throttle_classes = [throttles.LoginIpThrottle, throttles.LoginIdentifierThrottle]

    @extend_schema(
        tags=AUTH_TAG,
        summary="Sign in with email or mobile number and password",
        description=(
            "Starts a session and returns a 10-minute access token and a single-use refresh token. "
            "Unknown account, wrong password and inactive account all return the same "
            "`401 invalid_credentials`."
        ),
        request=s.PasswordLoginIn,
        responses={200: s.AuthSessionOut, **errors(400, 401, 429)},
    )
    def post(self, request: Request) -> Response:
        body = s.PasswordLoginIn(data=request.data)
        body.is_valid(raise_exception=True)
        issued = services.password_login(
            body.validated_data["identifier"],
            body.validated_data["password"],
            remember=body.validated_data["remember"],
        )
        return Response(_session_payload(issued))


class OtpRequestView(PublicAPIView):
    throttle_classes = [throttles.OtpRequestIpThrottle, throttles.OtpRequestPhoneThrottle]

    @extend_schema(
        tags=AUTH_TAG,
        summary="Send a one-time sign-in code by SMS",
        description=(
            "The response is identical whether or not the number has an account. "
            "Codes expire after 5 minutes and allow 5 attempts."
        ),
        request=s.OtpRequestIn,
        responses={200: s.OtpRequestOut, **errors(400, 429, 503)},
    )
    def post(self, request: Request) -> Response:
        body = s.OtpRequestIn(data=request.data)
        body.is_valid(raise_exception=True)
        result = otp.request_code(body.validated_data["phone"])
        data: dict[str, Any] = {
            "challenge_id": result.challenge_id,
            "expires_in": result.expires_in,
            "resend_in": result.resend_in,
        }
        if result.dev_code is not None:
            data["dev_code"] = result.dev_code
        return Response(data)


class OtpVerifyView(PublicAPIView):
    throttle_classes = [throttles.OtpVerifyIpThrottle, throttles.OtpVerifyChallengeThrottle]

    @extend_schema(
        tags=AUTH_TAG,
        summary="Sign in with a one-time code",
        description=(
            "Every failure (wrong, expired, used or locked code) returns the same `401 invalid_code`."
        ),
        request=s.OtpVerifyIn,
        responses={200: s.AuthSessionOut, **errors(400, 401, 429)},
    )
    def post(self, request: Request) -> Response:
        body = s.OtpVerifyIn(data=request.data)
        body.is_valid(raise_exception=True)
        user = otp.verify_code(str(body.validated_data["challenge_id"]), body.validated_data["code"])
        return Response(_session_payload(services.otp_login(user)))


class TokenRefreshView(PublicAPIView):
    throttle_classes = [throttles.RefreshIpThrottle]

    @extend_schema(
        tags=AUTH_TAG,
        summary="Rotate a refresh token",
        description=(
            "Returns a new access token **and a new refresh token**; the one sent is no longer valid. "
            "Sending an already-used refresh token is treated as theft: the whole session is revoked and the "
            "response is `401 not_authenticated`."
        ),
        request=s.RefreshIn,
        responses={200: s.TokenPairOut, **errors(400, 401, 429)},
    )
    def post(self, request: Request) -> Response:
        body = s.RefreshIn(data=request.data)
        body.is_valid(raise_exception=True)
        issued = tokens.rotate(body.validated_data["refresh"])
        return Response(
            {
                "access": issued.access,
                "refresh": issued.refresh,
                "token_type": "Bearer",
                "access_expires_in": issued.access_expires_in,
                "refresh_expires_at": issued.refresh_expires_at,
            }
        )


def _current_session(request: Request) -> AuthSession:
    session: AuthSession = request_user(request).auth_session  # type: ignore[attr-defined]
    return session


class LogoutView(AuthenticatedAPIView):
    allow_pending_password = True

    @extend_schema(
        tags=AUTH_TAG,
        summary="Sign out of this session",
        description=(
            "Revokes the session of the access token used, so its access and refresh tokens stop working "
            "immediately. Optionally also revokes the session of `refresh`, if it belongs to the same user."
        ),
        request=s.LogoutIn,
        responses={204: None, **errors(400, 401)},
    )
    def post(self, request: Request) -> Response:
        body = s.LogoutIn(data=request.data)
        body.is_valid(raise_exception=True)
        user = request_user(request)
        if raw := body.validated_data.get("refresh"):
            tokens.revoke_by_refresh(raw, user, RevokeReason.LOGOUT)
        services.logout(user, _current_session(request))
        return Response(status=204)


class LogoutAllView(AuthenticatedAPIView):
    allow_pending_password = True

    @extend_schema(
        tags=AUTH_TAG, summary="Sign out everywhere", request=None, responses={204: None, **errors(401)}
    )
    def post(self, request: Request) -> Response:
        services.logout_all(request_user(request))
        return Response(status=204)


class PasswordChangeView(AuthenticatedAPIView):
    allow_pending_password = True
    throttle_classes = [throttles.PasswordChangeUserThrottle]

    @extend_schema(
        tags=AUTH_TAG,
        summary="Change password",
        description="Signs out every other session. Clears `must_change_password`.",
        request=s.PasswordChangeIn,
        responses={204: None, **errors(400, 401, 429)},
    )
    def post(self, request: Request) -> Response:
        body = s.PasswordChangeIn(data=request.data)
        body.is_valid(raise_exception=True)
        services.change_password(
            request_user(request),
            body.validated_data.get("current_password", ""),
            body.validated_data["new_password"],
            keep=_current_session(request),
        )
        return Response(status=204)


class SessionListView(AuthenticatedAPIView):
    allow_pending_password = True

    @extend_schema(
        tags=AUTH_TAG,
        summary="List my active sessions",
        responses={200: s.SessionOut(many=True), **errors(401)},
    )
    def get(self, request: Request) -> Response:
        qs = AuthSession.objects.filter(
            user=request_user(request), revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).order_by("-last_used_at")
        context = {"current_session_id": _current_session(request).id}
        return Response(s.SessionOut(qs, many=True, context=context).data)


class SessionDetailView(AuthenticatedAPIView):
    allow_pending_password = True

    @extend_schema(
        tags=AUTH_TAG, summary="Revoke one of my sessions", responses={204: None, **errors(401, 404)}
    )
    def delete(self, request: Request, session_id: Any) -> Response:
        session = AuthSession.objects.filter(
            pk=session_id, user=request_user(request), revoked_at__isnull=True
        ).first()
        if session is None:
            raise NotFound()
        services.revoke_own_session(request_user(request), session)
        return Response(status=204)


class MeView(AuthenticatedAPIView):
    allow_pending_password = True

    @extend_schema(
        tags=["identity"],
        summary="The signed-in user and their schools",
        description="Lists active memberships in active schools, with roles. Needs no `X-School-Id`.",
        responses={200: s.MeOut, **errors(401)},
    )
    def get(self, request: Request) -> Response:
        user = request_user(request)
        return Response(
            {
                "user": s.UserOut(user).data,
                "memberships": s.MyMembershipOut(memberships_for_user(user), many=True).data,
            }
        )
