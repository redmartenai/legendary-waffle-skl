"""Roles, the permission catalogue and the caller's effective permissions."""

from __future__ import annotations

from typing import Any

from django.db.models import Prefetch, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.core.api import TENANT_HEADER, StrictSerializer, errors

from .. import services
from ..catalog import PERMISSIONS, DataScope
from ..grants import Actor
from ..models import Role, RolePermission
from .base import TenantAPIView

TAG = ["authz"]


class GrantField(serializers.DictField):
    """``{"attendance.read": ["section", "assigned"], ...}``"""

    child = serializers.ListField(child=serializers.ChoiceField(DataScope.choices), allow_empty=False)


class RoleOut(serializers.ModelSerializer[Role]):
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ("id", "key", "name", "is_system", "based_on", "permissions", "created_at", "updated_at")
        read_only_fields = fields

    def get_permissions(self, obj: Role) -> dict[str, list[str]]:
        return {g.permission_id: sorted(g.scopes) for g in obj.grants.all()}


class RoleCreateIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    based_on = serializers.UUIDField(required=False, help_text="Copy permissions from this role.")
    permissions = GrantField(required=False)


class RoleUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=100, required=False)
    permissions = GrantField(required=False, help_text="Replaces the role's whole permission set.")


class PermissionOut(serializers.Serializer[Any]):
    codename = serializers.CharField()
    description = serializers.CharField()


class MyPermissionsOut(serializers.Serializer[Any]):
    school_id = serializers.UUIDField()
    permissions = GrantField()


def _roles(actor: Actor) -> QuerySet[Role]:
    return Role.objects.for_school(actor.school).prefetch_related(
        Prefetch("grants", queryset=RolePermission.objects.order_by("permission_id"))
    )


def _role(actor: Actor, role_id: Any) -> Role:
    role = _roles(actor).filter(pk=role_id).first()
    if role is None:
        raise NotFound()
    return role


class PermissionCatalogView(TenantAPIView):
    required_permissions = {"GET": "permission.read"}

    @extend_schema(
        tags=TAG,
        summary="The permission catalogue",
        parameters=[TENANT_HEADER],
        responses={200: PermissionOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response([{"codename": c, "description": d} for c, d in sorted(PERMISSIONS.items())])


class MyPermissionsView(TenantAPIView):
    # Every member may see their own effective permissions (for UI hints; the server still enforces).
    required_permissions = {"GET": "school.read"}

    @extend_schema(
        tags=TAG,
        summary="My effective permissions in this school",
        description=(
            "The union of the caller's roles. For display only: every request is authorized server-side."
        ),
        parameters=[TENANT_HEADER],
        responses={200: MyPermissionsOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        grants = {c: sorted(s) for c, s in sorted(self.actor.grants.items())}
        return Response({"school_id": self.actor.school.pk, "permissions": grants})


class RoleListView(TenantAPIView):
    required_permissions = {"GET": "role.read", "POST": "role.create"}

    @extend_schema(
        tags=TAG,
        summary="List roles",
        parameters=[TENANT_HEADER],
        responses={200: RoleOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response(RoleOut(_roles(self.actor).order_by("is_system", "name"), many=True).data)

    @extend_schema(
        tags=TAG,
        summary="Create a custom role",
        description="Only permissions the caller holds with `school` scope can be granted.",
        parameters=[TENANT_HEADER],
        request=RoleCreateIn,
        responses={201: RoleOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request) -> Response:
        body = RoleCreateIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        based_on = _role(self.actor, data["based_on"]) if data.get("based_on") else None
        role = services.create_role(
            self.actor, name=data["name"], grants=data.get("permissions") or {}, based_on=based_on
        )
        return Response(RoleOut(_role(self.actor, role.pk)).data, status=201)


class RoleDetailView(TenantAPIView):
    required_permissions = {"GET": "role.read", "PATCH": "role.update", "DELETE": "role.delete"}

    @extend_schema(
        tags=TAG,
        summary="A role",
        parameters=[TENANT_HEADER],
        responses={200: RoleOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, role_id: Any) -> Response:
        return Response(RoleOut(_role(self.actor, role_id)).data)

    @extend_schema(
        tags=TAG,
        summary="Rename a role or replace its permissions",
        description=(
            "The `school_admin` role is locked. Only permissions the caller holds school-wide can change."
        ),
        parameters=[TENANT_HEADER],
        request=RoleUpdateIn,
        responses={200: RoleOut, **errors(400, 401, 403, 404)},
    )
    def patch(self, request: Request, role_id: Any) -> Response:
        role = _role(self.actor, role_id)
        body = RoleUpdateIn(data=request.data)
        body.is_valid(raise_exception=True)
        services.update_role(
            self.actor,
            role,
            name=body.validated_data.get("name"),
            grants=body.validated_data.get("permissions"),
        )
        return Response(RoleOut(_role(self.actor, role_id)).data)

    @extend_schema(
        tags=TAG,
        summary="Delete a custom role",
        parameters=[TENANT_HEADER],
        responses={204: None, **errors(401, 403, 404, 409)},
    )
    def delete(self, request: Request, role_id: Any) -> Response:
        services.delete_role(self.actor, _role(self.actor, role_id))
        return Response(status=204)
