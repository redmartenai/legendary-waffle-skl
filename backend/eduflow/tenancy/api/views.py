"""School and membership endpoints. All but the public lookup act in the ``X-School-Id`` school."""

from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from eduflow.authz import services as authz_services
from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.models import MembershipRole, Role
from eduflow.core.api import TENANT_HEADER, errors
from eduflow.identity.throttles import MemberCreateUserThrottle, SchoolLookupIpThrottle

from .. import selectors, services
from . import serializers as s

TAG = ["tenancy"]


class SchoolLookupView(APIView):
    """Public: the sign-in screen finds a school by its short code. Returns only public fields."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [SchoolLookupIpThrottle]

    @extend_schema(
        tags=TAG,
        summary="Find a school by code (public)",
        parameters=[OpenApiParameter("code", OpenApiTypes.STR, required=True)],
        responses={200: s.SchoolPublicOut, **errors(404, 429)},
    )
    def get(self, request: Request) -> Response:
        school = selectors.active_school_by_code(str(request.query_params.get("code", ""))[:32])
        if school is None:
            raise NotFound()
        return Response(s.SchoolPublicOut(school).data)


class CurrentSchoolView(TenantAPIView):
    required_permissions = {"GET": "school.read", "PATCH": "school.update"}

    @extend_schema(
        tags=TAG,
        summary="The current school",
        parameters=[TENANT_HEADER],
        responses={200: s.SchoolOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response(s.SchoolOut(self.actor.school).data)

    @extend_schema(
        tags=TAG,
        summary="Update the current school",
        parameters=[TENANT_HEADER],
        request=s.SchoolUpdateIn,
        responses={200: s.SchoolOut, **errors(400, 401, 403)},
    )
    def patch(self, request: Request) -> Response:
        body = s.SchoolUpdateIn(data=request.data)
        body.is_valid(raise_exception=True)
        school = services.update_school(self.actor.school, actor_id=self.actor.user.pk, **body.validated_data)
        return Response(s.SchoolOut(school).data)


def _roles_in_school(actor: Any, role_ids: list[Any]) -> list[Role]:
    roles = list(Role.objects.for_school(actor.school).filter(pk__in=role_ids))
    if len(roles) != len(set(role_ids)):
        raise ValidationError({"role_ids": ["Unknown role."]})
    return roles


class MembershipListView(TenantAPIView):
    required_permissions = {"GET": "user.read", "POST": "user.create"}

    def get_throttles(self) -> list[Any]:
        # Adding members looks accounts up by email and phone, so it is rate-limited per admin.
        return [MemberCreateUserThrottle()] if self.request.method == "POST" else []

    @extend_schema(
        tags=TAG,
        summary="List members (within the caller's data scope)",
        parameters=[TENANT_HEADER],
        responses={200: s.MembershipOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response(s.MembershipOut(selectors.member_list(self.actor), many=True).data)

    @extend_schema(
        tags=TAG,
        summary="Add a member",
        description=(
            "Creates an account (no password; the person signs in by phone OTP) or attaches an existing "
            "account, but only through a verified email or mobile number: otherwise `409`. An existing "
            "account's profile is never changed. Giving roles also requires `role.assign`, and only roles "
            "whose "
            "permissions the caller holds school-wide."
        ),
        parameters=[TENANT_HEADER],
        request=s.MemberCreateIn,
        responses={201: s.MembershipOut, **errors(400, 401, 403, 409)},
    )
    def post(self, request: Request) -> Response:
        body = s.MemberCreateIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        if data["role_ids"] and not self.actor.has("role.assign"):
            self.permission_denied(request)
        roles = _roles_in_school(self.actor, data["role_ids"])
        person = services.PersonSpec(
            full_name=data["full_name"],
            email=data.get("email"),
            phone=data.get("phone"),
        )
        membership = services.add_member(self.actor, person, roles)
        return Response(s.MembershipOut(selectors.member_detail(self.actor, membership.pk)).data, status=201)


class MembershipDetailView(TenantAPIView):
    required_permissions = {"GET": "user.read", "PATCH": "user.disable"}

    @extend_schema(
        tags=TAG,
        summary="A member",
        parameters=[TENANT_HEADER],
        responses={200: s.MembershipOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, membership_id: Any) -> Response:
        return Response(s.MembershipOut(selectors.member_detail(self.actor, membership_id)).data)

    @extend_schema(
        tags=TAG,
        summary="Activate or deactivate a member in this school",
        parameters=[TENANT_HEADER],
        request=s.MembershipUpdateIn,
        responses={200: s.MembershipOut, **errors(400, 401, 403, 404, 409)},
    )
    def patch(self, request: Request, membership_id: Any) -> Response:
        membership = selectors.member_detail(self.actor, membership_id, "user.disable")
        body = s.MembershipUpdateIn(data=request.data)
        body.is_valid(raise_exception=True)
        services.set_membership_active(self.actor, membership, body.validated_data["is_active"])
        return Response(
            s.MembershipOut(selectors.member_detail(self.actor, membership_id, "user.disable")).data
        )


class MembershipRoleListView(TenantAPIView):
    required_permissions = {"POST": "role.assign"}

    @extend_schema(
        tags=TAG,
        summary="Give a member a role",
        parameters=[TENANT_HEADER],
        request=s.RoleAssignIn,
        responses={201: s.MembershipOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, membership_id: Any) -> Response:
        membership = selectors.member_detail(self.actor, membership_id, "role.assign")
        body = s.RoleAssignIn(data=request.data)
        body.is_valid(raise_exception=True)
        [role] = _roles_in_school(self.actor, [body.validated_data["role_id"]])
        authz_services.assign_role(
            self.actor,
            membership,
            role,
            title=body.validated_data["title"],
            department=body.validated_data["department"],
        )
        return Response(
            s.MembershipOut(selectors.member_detail(self.actor, membership_id, "role.assign")).data,
            status=201,
        )


class MembershipRoleDetailView(TenantAPIView):
    required_permissions = {"DELETE": "role.assign"}

    @extend_schema(
        tags=TAG,
        summary="Remove a role from a member",
        parameters=[TENANT_HEADER],
        responses={204: None, **errors(401, 403, 404, 409)},
    )
    def delete(self, request: Request, membership_id: Any, role_id: Any) -> Response:
        membership = selectors.member_detail(self.actor, membership_id, "role.assign")
        assignment = (
            MembershipRole.objects.for_school(self.actor.school)
            .select_related("role")
            .filter(membership=membership, role_id=role_id)
            .first()
        )
        if assignment is None:
            raise NotFound()
        authz_services.unassign_role(self.actor, assignment)
        return Response(status=204)
