from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.identity.api.serializers import MembershipRoleOut

from ..models import Membership, School, validate_timezone


class SchoolPublicOut(serializers.ModelSerializer[School]):
    class Meta:
        model = School
        fields = ("id", "code", "name")
        read_only_fields = fields


SCHOOL_PROFILE_FIELDS = (
    "legal_name",
    "short_name",
    "email",
    "phone",
    "website",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
    "timezone",
    "settings",
)


class SchoolOut(serializers.ModelSerializer[School]):
    class Meta:
        model = School
        fields = ("id", "code", "name", *SCHOOL_PROFILE_FIELDS, "is_active", "created_at", "updated_at")
        read_only_fields = fields


def _timezone(value: str) -> None:
    try:
        validate_timezone(value)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.messages) from None


class SchoolUpdateIn(StrictSerializer):
    """The school's own profile. The code and active flag are platform-controlled."""

    name = serializers.CharField(max_length=200, required=False)
    legal_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    short_name = serializers.CharField(max_length=50, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    website = serializers.URLField(required=False, allow_blank=True)
    address_line1 = serializers.CharField(max_length=200, required=False, allow_blank=True)
    address_line2 = serializers.CharField(max_length=200, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    postal_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    country = serializers.RegexField(r"^[A-Z]{2}$", required=False, help_text="ISO 3166-1 alpha-2, e.g. IN.")
    timezone = serializers.CharField(max_length=64, required=False, validators=[_timezone])
    settings = serializers.DictField(required=False, help_text="School preferences. Never credentials.")


class MemberUserOut(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    full_name = serializers.CharField()
    email = serializers.EmailField(allow_null=True)
    phone = serializers.CharField(allow_null=True)
    is_active = serializers.BooleanField()


class MembershipOut(serializers.ModelSerializer[Membership]):
    user = MemberUserOut()
    roles = MembershipRoleOut(many=True, source="role_assignments.all")

    class Meta:
        model = Membership
        fields = ("id", "user", "is_active", "roles", "created_at")
        read_only_fields = fields


class MemberCreateIn(StrictSerializer):
    full_name = serializers.CharField(max_length=200)
    email = serializers.EmailField(required=False)
    phone = serializers.CharField(max_length=32, required=False)
    role_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list, max_length=20
    )


class MembershipUpdateIn(StrictSerializer):
    is_active = serializers.BooleanField()


class RoleAssignIn(StrictSerializer):
    role_id = serializers.UUIDField()
    title = serializers.CharField(max_length=100, required=False, default="")
    department = serializers.CharField(max_length=100, required=False, default="")
