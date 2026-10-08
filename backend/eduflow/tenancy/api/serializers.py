from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.identity.api.serializers import MembershipRoleOut

from ..models import Membership, School


class SchoolPublicOut(serializers.ModelSerializer[School]):
    class Meta:
        model = School
        fields = ("id", "code", "name")
        read_only_fields = fields


class SchoolOut(serializers.ModelSerializer[School]):
    class Meta:
        model = School
        fields = ("id", "code", "name", "is_active", "created_at", "updated_at")
        read_only_fields = fields


class SchoolUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=200, required=False)


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
