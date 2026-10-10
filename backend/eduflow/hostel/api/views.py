"""Hostel endpoints. ``hostel.read`` (families: their children's allocation, outpasses and roll call),
``hostel.manage`` (the hostel office, school-wide), ``hostel.outpass`` (request an outpass: parents for
their children, students for themselves) and ``hostel.approve`` (decide in the approvals queue)."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Prefetch, Q, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.api.resources import (
    Filter,
    ResourceDetailView,
    ResourceListView,
    ResourceView,
    document_resource,
)
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors

from .. import policies, services
from ..models import Allocation, Hostel, Outpass, OutpassStatus, RollCall, Room
from . import serializers as s

TAG = "hostel"
READ, MANAGE = "hostel.read", "hostel.manage"


def _school_wide(view: TenantAPIView) -> None:
    if DataScope.SCHOOL not in view.actor.scopes(MANAGE):
        view.permission_denied(view.request)


class _Hostel(ResourceView):
    tag = TAG
    resource = policies.hostels
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.HostelOut

    def base_queryset(self) -> QuerySet[Hostel]:
        rooms = Room.objects.annotate(
            occupied=Count("allocations", filter=Q(allocations__end_date__isnull=True))
        )
        return Hostel.objects.select_related("warden__user").prefetch_related(
            Prefetch("rooms", queryset=rooms)
        )


@document_resource
class HostelList(_Hostel, ResourceListView):
    create_serializer = s.HostelIn

    def perform_create(self, data: dict[str, Any]) -> Hostel:
        return services.create_hostel(self.actor, **data)


@document_resource
class HostelDetail(_Hostel, ResourceDetailView):
    pass


class HostelRoomsView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Add a room",
        parameters=[TENANT_HEADER],
        request=s.HostelRoomIn,
        responses={201: s.HostelRoomOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self)
        hostel = policies.hostels.get(self.actor, MANAGE, pk)
        body = s.HostelRoomIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.HostelRoomOut(services.add_room(self.actor, hostel, **body.validated_data)).data, status=201
        )


class HostelRollView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Record the roll call of a hostel for a night",
        parameters=[TENANT_HEADER],
        request=s.RollIn,
        responses={200: s.RecordedOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self)
        hostel = policies.hostels.get(self.actor, MANAGE, pk)
        body = s.RollIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response({"recorded": services.roll_call(self.actor, hostel, **body.validated_data)})


class _Allocation(ResourceView):
    tag = TAG
    resource = policies.allocations
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.AllocationOut

    def base_queryset(self) -> QuerySet[Allocation]:
        return Allocation.objects.select_related("student", "room__hostel")


def _active(qs: QuerySet[Any], value: bool) -> QuerySet[Any]:
    return qs.filter(end_date__isnull=value)


@document_resource
class AllocationList(_Allocation, ResourceListView):
    create_serializer = s.AllocationIn
    filters = [
        Filter("active", "", serializers.BooleanField(), apply=_active),
        Filter("student_id", "student_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Allocation:
        return services.allocate(self.actor, **data)


@document_resource
class AllocationDetail(_Allocation, ResourceDetailView):
    allow_delete = True

    def perform_delete(self, obj: Allocation) -> None:
        services.vacate(self.actor, obj)


class _Outpass(ResourceView):
    tag = TAG
    resource = policies.outpasses
    read_permission, write_permission = READ, "hostel.outpass"
    output_serializer = s.OutpassOut
    narrow_writes = True  # parents and students request for themselves (checked in the service)

    def base_queryset(self) -> QuerySet[Outpass]:
        return Outpass.objects.select_related("student")


@document_resource
class OutpassList(_Outpass, ResourceListView):
    create_serializer = s.OutpassIn
    filters = [
        Filter("status", "status", serializers.ChoiceField(OutpassStatus.choices)),
        Filter("student_id", "student_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Outpass:
        return services.request_outpass(self.actor, **data)


@document_resource
class OutpassDetail(_Outpass, ResourceDetailView):
    pass


class OutpassGateView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Check a student out on an approved outpass, or back in",
        parameters=[TENANT_HEADER],
        request=s.GateIn,
        responses={200: s.OutpassOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self)
        outpass = policies.outpasses.get(self.actor, MANAGE, pk)
        body = s.GateIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(s.OutpassOut(services.gate(self.actor, outpass, **body.validated_data)).data)


class OutpassCancelView(TenantAPIView):
    required_permissions = {"POST": "hostel.outpass"}

    @extend_schema(
        tags=[TAG],
        summary="Cancel an outpass that is pending or approved",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.OutpassOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        outpass = policies.outpasses.get(self.actor, "hostel.outpass", pk)
        return Response(s.OutpassOut(services.cancel_outpass(self.actor, outpass)).data)


class _Roll(ResourceView):
    tag = TAG
    resource = policies.roll
    read_permission = READ
    output_serializer = s.RollOut

    def base_queryset(self) -> QuerySet[RollCall]:
        return RollCall.objects.select_related("student")


@document_resource
class RollList(_Roll, ResourceListView):
    filters = [
        Filter("date", "date", serializers.DateField()),
        Filter("hostel_id", "hostel_id", serializers.UUIDField()),
        Filter("student_id", "student_id", serializers.UUIDField()),
    ]
