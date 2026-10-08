"""Token lifecycle (ADR-005, docs/security/token-lifecycle.md).

* **Access token**: a JWT signed by ``djangorestframework-simplejwt`` (HS256), valid for
  ``ACCESS_TOKEN_LIFETIME`` (10 minutes). Claims: ``sub`` (user), ``sid`` (session), ``jti``, ``exp``,
  ``iat``, ``iss``, ``aud``, ``token_type``. No school and no role: those are resolved per request.
* **Refresh token**: 48 random bytes (``secrets``), opaque to the client. Only its SHA-256 is stored. Every
  refresh *rotates* it: the presented token is marked used and a new one is issued in the same session.
* **Reuse detection**: presenting a token that was already rotated means a copy exists outside the legitimate
  client. The whole session (token family) is revoked, which also kills its access tokens, and the event is
  audited.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from eduflow.audit import services as audit
from eduflow.audit.models import Outcome
from eduflow.core.api import InvalidRefreshToken
from eduflow.core.logging import get_logger
from eduflow.core.request_context import get_request_info

from .models import AuthMethod, AuthSession, RefreshToken, RevokeReason, User

log = get_logger(__name__)


@dataclass(frozen=True)
class IssuedTokens:
    access: str
    refresh: str
    access_expires_in: int
    refresh_expires_at: datetime
    session: AuthSession


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _refresh_lifetime(remember: bool) -> timedelta:
    return settings.REFRESH_TOKEN_REMEMBER_LIFETIME if remember else settings.REFRESH_TOKEN_LIFETIME


def _new_access(session: AuthSession) -> str:
    token = AccessToken.for_user(session.user)
    token["sid"] = str(session.id)
    return str(token)


def _new_refresh(session: AuthSession, now: datetime) -> tuple[str, RefreshToken]:
    raw = secrets.token_urlsafe(48)
    expires_at = min(now + _refresh_lifetime(session.remember), session.expires_at)
    row = RefreshToken.objects.create(session=session, token_hash=hash_token(raw), expires_at=expires_at)
    return raw, row


def _issued(session: AuthSession, raw_refresh: str, row: RefreshToken) -> IssuedTokens:
    return IssuedTokens(
        access=_new_access(session),
        refresh=raw_refresh,
        access_expires_in=int(settings.ACCESS_TOKEN_LIFETIME.total_seconds()),
        refresh_expires_at=row.expires_at,
        session=session,
    )


def start_session(user: User, *, method: AuthMethod, remember: bool = False) -> IssuedTokens:
    """Create a session (token family) and its first token pair. Callers audit the sign-in."""
    now = timezone.now()
    info = get_request_info()
    with transaction.atomic():
        session = AuthSession.objects.create(
            user=user,
            auth_method=method,
            remember=remember,
            last_used_at=now,
            expires_at=now + settings.AUTH_SESSION_MAX_AGE,
            ip=info.ip,
            user_agent=info.user_agent,
        )
        raw, row = _new_refresh(session, now)
        User.objects.filter(pk=user.pk).update(last_login=now)
    return _issued(session, raw, row)


def _revoke(session: AuthSession, reason: RevokeReason, now: datetime) -> None:
    AuthSession.objects.filter(pk=session.pk, revoked_at__isnull=True).update(
        revoked_at=now, revoke_reason=reason
    )


def _rejection(token: RefreshToken, now: datetime) -> str | None:
    session = token.session
    if session.revoked_at is not None:
        return "revoked"
    if token.used_at is not None:
        return "reused"
    if token.expires_at <= now or session.expires_at <= now:
        return "expired"
    if not session.user.is_active:
        return "inactive"
    return None


def rotate(raw_refresh: str) -> IssuedTokens:
    """Exchange a refresh token for a new pair. Raises ``InvalidRefreshToken`` (401) for any failure.

    Failure handling commits *before* raising, so revocation and audit records survive the error response.
    """
    now = timezone.now()
    with transaction.atomic():
        token = (
            RefreshToken.objects.select_for_update(of=("self", "session"))
            .select_related("session", "session__user")
            .filter(token_hash=hash_token(raw_refresh or ""))
            .first()
        )
        if token is None:
            audit.record(
                "auth.refresh", outcome=Outcome.FAILURE, actor_id=None, metadata={"reason": "unknown"}
            )
            failure: str | None = "unknown"
        else:
            failure = _rejection(token, now)
            session = token.session
            if failure == "reused":
                _revoke(session, RevokeReason.REUSE_DETECTED, now)
                log.warning("refresh_token_reuse_detected", session_id=str(session.id))
                audit.record(
                    "auth.refresh.reuse_detected",
                    outcome=Outcome.FAILURE,
                    actor_id=session.user_id,
                    school_id=None,
                    target_type="auth_session",
                    target_id=session.id,
                )
            elif failure is not None:
                if failure == "inactive":
                    _revoke(session, RevokeReason.USER_DEACTIVATED, now)
                audit.record(
                    "auth.refresh",
                    outcome=Outcome.FAILURE,
                    actor_id=session.user_id,
                    school_id=None,
                    target_type="auth_session",
                    target_id=session.id,
                    metadata={"reason": failure},
                )
            else:
                raw, new_row = _new_refresh(session, now)
                token.used_at = now
                token.replaced_by = new_row
                token.save(update_fields=["used_at", "replaced_by"])
                AuthSession.objects.filter(pk=session.pk).update(last_used_at=now)
                audit.record(
                    "auth.refresh",
                    actor_id=session.user_id,
                    school_id=None,
                    target_type="auth_session",
                    target_id=session.id,
                )
                return _issued(session, raw, new_row)
    raise InvalidRefreshToken()


def revoke_session(session: AuthSession, reason: RevokeReason) -> None:
    _revoke(session, reason, timezone.now())


def revoke_by_refresh(raw_refresh: str, user: User | None, reason: RevokeReason) -> AuthSession | None:
    """Revoke the session a refresh token belongs to (when it belongs to ``user``, if one is given)."""
    token = RefreshToken.objects.select_related("session").filter(token_hash=hash_token(raw_refresh)).first()
    if token is None or (user is not None and token.session.user_id != user.pk):
        return None
    revoke_session(token.session, reason)
    return token.session


def revoke_all(user: User, reason: RevokeReason, *, keep: AuthSession | None = None) -> int:
    qs = AuthSession.objects.filter(user=user, revoked_at__isnull=True)
    if keep is not None:
        qs = qs.exclude(pk=keep.pk)
    return qs.update(revoked_at=timezone.now(), revoke_reason=reason)
