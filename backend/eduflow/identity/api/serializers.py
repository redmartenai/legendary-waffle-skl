from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer

from ..models import AuthSession, User
from ..phone import InvalidPhone, normalize_phone


class PasswordLoginIn(StrictSerializer):
    identifier = serializers.CharField(max_length=254, help_text="Email address or mobile number.")
    password = serializers.CharField(max_length=256, trim_whitespace=False, style={"input_type": "password"})
    remember = serializers.BooleanField(default=False)


class PhoneField(serializers.CharField):
    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        try:
            return normalize_phone(value)
        except InvalidPhone as exc:
            raise serializers.ValidationError(str(exc)) from None


class OtpRequestIn(StrictSerializer):
    phone = PhoneField(max_length=32)


class OtpRequestOut(serializers.Serializer[Any]):
    challenge_id = serializers.UUIDField()
    expires_in = serializers.IntegerField(help_text="Seconds until the code expires.")
    resend_in = serializers.IntegerField(help_text="Seconds before another code can be requested.")
    dev_code = serializers.CharField(
        required=False, help_text="Local development only (DEBUG and OTP_ECHO_DEV_CODE). Never in production."
    )


class OtpVerifyIn(StrictSerializer):
    challenge_id = serializers.UUIDField()
    code = serializers.RegexField(r"^[0-9]{4,8}$", max_length=8)


class RefreshIn(StrictSerializer):
    refresh = serializers.CharField(max_length=128)


class LogoutIn(StrictSerializer):
    refresh = serializers.CharField(max_length=128, required=False)


class PasswordChangeIn(StrictSerializer):
    current_password = serializers.CharField(
        max_length=256,
        trim_whitespace=False,
        required=False,
        allow_blank=True,
        help_text="Required unless the account has no password yet (e.g. created by a school; OTP sign-in).",
    )
    new_password = serializers.CharField(max_length=256, trim_whitespace=False)


class UserOut(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = (
            "id",
            "full_name",
            "email",
            "phone",
            "language",
            "is_platform_admin",
            "must_change_password",
        )
        read_only_fields = fields


class MembershipRoleOut(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="role.id")
    key = serializers.CharField(source="role.key")
    name = serializers.CharField(source="role.name")
    title = serializers.CharField()
    department = serializers.CharField()


class SchoolRefOut(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


class MyMembershipOut(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    school = SchoolRefOut()
    roles = MembershipRoleOut(many=True, source="role_assignments.all")


class TokenPairOut(serializers.Serializer[Any]):
    access = serializers.CharField(help_text="JWT access token. Send as `Authorization: Bearer <access>`.")
    refresh = serializers.CharField(
        help_text="Opaque refresh token. Single use: each refresh returns a new one."
    )
    token_type = serializers.CharField(default="Bearer")
    access_expires_in = serializers.IntegerField(help_text="Seconds (600).")
    refresh_expires_at = serializers.DateTimeField()


class AuthSessionOut(TokenPairOut):
    user = UserOut()
    memberships = MyMembershipOut(many=True)


class MeOut(serializers.Serializer[Any]):
    user = UserOut()
    memberships = MyMembershipOut(many=True)


class SessionOut(serializers.ModelSerializer[AuthSession]):
    current = serializers.SerializerMethodField()

    class Meta:
        model = AuthSession
        fields = (
            "id",
            "auth_method",
            "remember",
            "created_at",
            "last_used_at",
            "expires_at",
            "user_agent",
            "current",
        )
        read_only_fields = fields

    def get_current(self, obj: AuthSession) -> bool:
        return bool(self.context.get("current_session_id") == obj.id)
