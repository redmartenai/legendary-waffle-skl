"""Identity: who a person is and how they prove it (docs/security/authentication.md).

These tables are platform-owned, not school-owned, so they carry no ``school_id`` and no RLS policy. What a
user may do inside a school lives in ``tenancy`` (membership) and ``authz`` (roles and permissions).
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from eduflow.core.ids import uuid7

from .phone import normalize_phone


class UserManager(BaseUserManager["User"]):
    def create_user(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        password: str | None = None,
        **extra: Any,
    ) -> User:
        user = self.model(
            email=self.normalize_email_address(email),
            phone=normalize_phone(phone) if phone else None,
            **extra,
        )
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    @staticmethod
    def normalize_email_address(email: str | None) -> str | None:
        email = (email or "").strip()
        return email.lower() or None


class User(AbstractBaseUser):
    """A person. One account can hold memberships in several schools.

    Sign-in identifiers are email (case-insensitive, stored lower-case) and phone (E.164). At least one is
    required. Django's per-user permission tables are deliberately not used: authorization is the EduFlow
    RBAC model (``authz``), and platform administration is the ``is_platform_admin`` flag (ADR-004).
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.EmailField(max_length=254, unique=True, null=True, blank=True)
    phone = models.CharField(max_length=16, unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    is_platform_admin = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    language = models.CharField(max_length=8, default="en")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    # Set when the person proved control of the identifier (phone: a successful OTP sign-in) or when it was
    # entered by a trusted party (platform staff). Schools can only attach an *existing* account through a
    # verified identifier, so nobody can pre-register someone else's email or phone and capture the
    # memberships other schools later grant to it (docs/security/multitenancy.md#adding-members).
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["full_name"]

    objects: ClassVar[UserManager] = UserManager()

    class Meta:
        db_table = "identity_user"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email__isnull=False) | models.Q(phone__isnull=False),
                name="identity_user_email_or_phone_check",
            ),
            models.CheckConstraint(
                condition=models.Q(email__isnull=True) | models.Q(email=Lower("email")),
                name="identity_user_email_lowercase_check",
            ),
            models.CheckConstraint(
                condition=models.Q(phone__isnull=True) | models.Q(phone__regex=r"^\+[1-9][0-9]{7,14}$"),
                name="identity_user_phone_e164_check",
            ),
        ]

    def __str__(self) -> str:
        return str(self.id)


class AuthMethod(models.TextChoices):
    PASSWORD = "password", "Password"
    OTP = "otp", "One-time code"
    INVITATION = "invitation", "Accepted an invitation (one-time code)"


class RevokeReason(models.TextChoices):
    LOGOUT = "logout", "Signed out"
    LOGOUT_ALL = "logout_all", "Signed out everywhere"
    REUSE_DETECTED = "reuse_detected", "Refresh token reuse detected"
    PASSWORD_CHANGED = "password_changed", "Password changed"
    USER_DEACTIVATED = "user_deactivated", "Account deactivated"
    SESSION_REVOKED = "session_revoked", "Revoked by the user"


class AuthSession(models.Model):
    """One sign-in on one device: the *family* of refresh tokens produced by rotation.

    Its ID is the ``sid`` claim of every access token issued in it, so revoking the session invalidates
    the access tokens immediately as well as the refresh tokens (docs/security/token-lifecycle.md).
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="auth_sessions")
    auth_method = models.CharField(max_length=16, choices=AuthMethod.choices)
    remember = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField()
    expires_at = models.DateTimeField(help_text="Absolute end of the session, whatever the rotation.")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=32, choices=RevokeReason.choices, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)

    class Meta:
        db_table = "identity_auth_session"
        indexes = [
            # Session listing and "revoke all" for a user.
            models.Index(fields=["user", "revoked_at"], name="identity_auth_session_user_idx"),
        ]

    def __str__(self) -> str:
        return str(self.id)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()


class RefreshToken(models.Model):
    """One refresh token. Only the SHA-256 of the token is stored; the token itself is shown once.

    ``used_at`` is set when the token is rotated. Presenting a token whose ``used_at`` is set is reuse: either
    the legitimate client or an attacker holds a stolen copy, and the whole session is revoked.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    session = models.ForeignKey(AuthSession, on_delete=models.CASCADE, related_name="refresh_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.OneToOneField(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replaces"
    )

    class Meta:
        db_table = "identity_refresh_token"

    def __str__(self) -> str:
        return str(self.id)


class OtpPurpose(models.TextChoices):
    LOGIN = "login", "Sign in"
    INVITATION = "invitation", "Accept an invitation"


class OtpChallenge(models.Model):
    """A one-time code sent to a phone or email address. Neither the code nor the address is stored.

    ``code_hash`` is an HMAC of the challenge ID and the code, and ``address_hash`` an HMAC of the channel and
    address (used for cooldowns). A challenge is bound to its ``purpose`` and, for invitations, to the
    invitation it verifies (``subject_id``): a code issued for one purpose or subject never verifies another.
    Sign-in challenges are also created for numbers with no account, so the request endpoint cannot be used to
    discover which numbers are registered; such a challenge can never be verified.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    purpose = models.CharField(max_length=16, choices=OtpPurpose.choices, default=OtpPurpose.LOGIN)
    address_hash = models.CharField(max_length=64)
    subject_id = models.UUIDField(
        null=True, blank=True, help_text="The invitation an invitation code verifies."
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "identity_otp_challenge"
        indexes = [
            # Cooldown and "invalidate previous challenges" lookups.
            models.Index(fields=["address_hash", "created_at"], name="identity_otp_address_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(attempts__lte=models.F("max_attempts")),
                name="identity_otp_challenge_attempts_max_check",
            ),
        ]

    def __str__(self) -> str:
        return str(self.id)
