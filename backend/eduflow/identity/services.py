"""Identity write operations: sign-in, password change, account activation. Each one is audited."""

from __future__ import annotations

from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from eduflow.audit import services as audit
from eduflow.audit.models import Outcome
from eduflow.core.api import InvalidCredentials

from . import tokens
from .models import AuthMethod, AuthSession, RevokeReason, User
from .phone import InvalidPhone, normalize_phone


def check_password_strength(password: str, user: User | None, *, field: str) -> None:
    """Django's password validators, reported as a normal ``validation_error`` on ``field``."""
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise ValidationError({field: list(exc.messages)}) from None


def find_user_by_identifier(identifier: str) -> User | None:
    identifier = identifier.strip()
    if "@" in identifier:
        return User.objects.filter(email=identifier.lower()).first()
    try:
        return User.objects.filter(phone=normalize_phone(identifier)).first()
    except InvalidPhone:
        return None


def password_login(identifier: str, password: str, *, remember: bool) -> tokens.IssuedTokens:
    """One generic failure for unknown account, wrong password and inactive account (no enumeration)."""
    user = find_user_by_identifier(identifier)
    if user is None:
        # Hash anyway, so an unknown identifier takes as long as a wrong password (Argon2 dominates).
        make_password(password)
        ok = False
    else:
        ok = user.check_password(password) and user.is_active

    if not ok or user is None:
        audit.record(
            "auth.login",
            outcome=Outcome.FAILURE,
            actor_id=user.pk if user else None,
            school_id=None,
            metadata={"method": AuthMethod.PASSWORD},
        )
        raise InvalidCredentials()

    issued = tokens.start_session(user, method=AuthMethod.PASSWORD, remember=remember)
    audit.record(
        "auth.login",
        actor_id=user.pk,
        school_id=None,
        target_type="auth_session",
        target_id=issued.session.id,
        metadata={"method": AuthMethod.PASSWORD, "remember": remember},
    )
    return issued


def otp_login(user: User) -> tokens.IssuedTokens:
    issued = tokens.start_session(user, method=AuthMethod.OTP)
    audit.record(
        "auth.login",
        actor_id=user.pk,
        school_id=None,
        target_type="auth_session",
        target_id=issued.session.id,
        metadata={"method": AuthMethod.OTP},
    )
    return issued


def logout(user: User, session: AuthSession) -> None:
    tokens.revoke_session(session, RevokeReason.LOGOUT)
    audit.record(
        "auth.logout", actor_id=user.pk, school_id=None, target_type="auth_session", target_id=session.id
    )


def logout_all(user: User) -> int:
    count = tokens.revoke_all(user, RevokeReason.LOGOUT_ALL)
    audit.record("auth.logout_all", actor_id=user.pk, school_id=None, metadata={"sessions": count})
    return count


def revoke_own_session(user: User, session: AuthSession) -> None:
    tokens.revoke_session(session, RevokeReason.SESSION_REVOKED)
    audit.record(
        "auth.session.revoked",
        actor_id=user.pk,
        school_id=None,
        target_type="auth_session",
        target_id=session.id,
    )


def change_password(user: User, current: str, new: str, *, keep: AuthSession | None) -> None:
    """Every other session is signed out, so a password change evicts anyone holding a stolen token."""
    if not user.check_password(current):
        audit.record("identity.password.change", outcome=Outcome.FAILURE, actor_id=user.pk, school_id=None)
        raise InvalidCredentials()
    check_password_strength(new, user, field="new_password")
    with transaction.atomic():
        user.set_password(new)
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password", "updated_at"])
        revoked = tokens.revoke_all(user, RevokeReason.PASSWORD_CHANGED, keep=keep)
        audit.record(
            "identity.password.change",
            actor_id=user.pk,
            school_id=None,
            target_type="user",
            target_id=user.pk,
            metadata={"other_sessions_revoked": revoked},
        )


def set_user_active(user: User, active: bool, *, actor: User) -> User:
    """Platform-level activation. Deactivation signs the user out everywhere at once."""
    if user.is_active == active:
        return user
    with transaction.atomic():
        user.is_active = active
        user.deactivated_at = None if active else timezone.now()
        user.save(update_fields=["is_active", "deactivated_at", "updated_at"])
        if not active:
            tokens.revoke_all(user, RevokeReason.USER_DEACTIVATED)
        audit.record(
            "identity.user.activated" if active else "identity.user.deactivated",
            actor_id=actor.pk,
            school_id=None,
            target_type="user",
            target_id=user.pk,
        )
    return user
