"""Bearer access-token authentication.

simplejwt verifies the signature, expiry, issuer, audience and token type. On top of that, every request
checks the token's session (``sid``) in the database: a revoked or expired session, or an inactive user,
fails at once rather than when the token expires. Every failure is the same generic 401.
"""

from __future__ import annotations

import uuid

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.request import Request
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import Token

from eduflow.core import db_context
from eduflow.core.request_context import bind_request_info

from .models import AuthSession, User


class AccessTokenAuthentication(JWTAuthentication):
    www_authenticate_realm = "api"

    def get_user(self, validated_token: Token) -> User:  # type: ignore[override]
        try:
            session_id = uuid.UUID(str(validated_token.get("sid")))
            user_id = uuid.UUID(str(validated_token.get("sub")))
        except ValueError:
            raise AuthenticationFailed() from None
        session = (
            AuthSession.objects.select_related("user")
            .filter(
                pk=session_id,
                user_id=user_id,
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
                user__is_active=True,
            )
            .first()
        )
        if session is None:
            raise AuthenticationFailed()
        user = session.user
        user.auth_session = session  # type: ignore[attr-defined]
        bind_request_info(user_id=str(user.pk))
        if db_context.current() is not None:
            db_context.update(user_id=user.pk)
        return user


def request_user(request: Request) -> User:
    """The authenticated user of a request, typed. Views behind ``IsAuthenticated`` always have one."""
    user = request.user
    if not isinstance(user, User):
        raise NotAuthenticated()
    return user
