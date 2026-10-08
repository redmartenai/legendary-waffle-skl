"""Platform administration (``/api/v1/platform/*``): EduFlow staff only, across schools, always audited.

Platform administrators have no implicit access to school data through the ordinary endpoints (ADR-003);
these endpoints are their only cross-tenant door.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import PlatformAPIView
from eduflow.core.api import StrictSerializer, errors
from eduflow.identity import services as identity_services
from eduflow.identity.api.serializers import UserOut
from eduflow.identity.authentication import request_user
from eduflow.identity.models import User

from .. import services
from ..models import School
from .serializers import SchoolOut

TAG = ["platform"]


class AdminPersonIn(StrictSerializer):
    full_name = serializers.CharField(max_length=200)
    email = serializers.EmailField(required=False)
    phone = serializers.CharField(max_length=32, required=False)
    temporary_password = serializers.CharField(max_length=256, required=False, trim_whitespace=False)


class SchoolCreateIn(StrictSerializer):
    code = serializers.RegexField(r"^[a-z0-9][a-z0-9-]{1,31}$", max_length=32)
    name = serializers.CharField(max_length=200)
    admin = AdminPersonIn(required=False, help_text="The first school admin. Optional.")


class SchoolPatchIn(StrictSerializer):
    name = serializers.CharField(max_length=200, required=False)
    is_active = serializers.BooleanField(required=False)


class UserPatchIn(StrictSerializer):
    is_active = serializers.BooleanField()


def _school(school_id: Any) -> School:
    school = School.objects.filter(pk=school_id).first()
    if school is None:
        raise NotFound()
    return school


class PlatformSchoolListView(PlatformAPIView):
    @extend_schema(
        tags=TAG, summary="List schools", responses={200: SchoolOut(many=True), **errors(401, 403)}
    )
    def get(self, request: Request) -> Response:
        return Response(SchoolOut(School.objects.order_by("code"), many=True).data)

    @extend_schema(
        tags=TAG,
        summary="Create a school",
        description="Creates the school with its system roles and, optionally, its first school admin.",
        request=SchoolCreateIn,
        responses={201: SchoolOut, **errors(400, 401, 403, 409)},
    )
    def post(self, request: Request) -> Response:
        body = SchoolCreateIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        admin = services.PersonSpec(**data["admin"]) if data.get("admin") else None
        school = services.create_school(
            code=data["code"], name=data["name"], admin=admin, actor=request_user(request)
        )
        return Response(SchoolOut(school).data, status=201)


class PlatformSchoolDetailView(PlatformAPIView):
    @extend_schema(tags=TAG, summary="A school", responses={200: SchoolOut, **errors(401, 403, 404)})
    def get(self, request: Request, school_id: Any) -> Response:
        return Response(SchoolOut(_school(school_id)).data)

    @extend_schema(
        tags=TAG,
        summary="Rename, activate or deactivate a school",
        description="A deactivated school is unreachable for all its members at once.",
        request=SchoolPatchIn,
        responses={200: SchoolOut, **errors(400, 401, 403, 404)},
    )
    def patch(self, request: Request, school_id: Any) -> Response:
        body = SchoolPatchIn(data=request.data)
        body.is_valid(raise_exception=True)
        school = services.update_school(
            _school(school_id), actor_id=request_user(request).pk, **body.validated_data
        )
        return Response(SchoolOut(school).data)


class PlatformUserDetailView(PlatformAPIView):
    @extend_schema(
        tags=TAG,
        summary="Activate or deactivate an account",
        description="Deactivation signs the user out of every session immediately.",
        request=UserPatchIn,
        responses={200: UserOut, **errors(400, 401, 403, 404)},
    )
    def patch(self, request: Request, user_id: Any) -> Response:
        body = UserPatchIn(data=request.data)
        body.is_valid(raise_exception=True)
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            raise NotFound()
        if user.pk == request_user(request).pk:
            raise PermissionDenied("You cannot deactivate your own account.")
        identity_services.set_user_active(user, body.validated_data["is_active"], actor=request_user(request))
        return Response(UserOut(user).data)
