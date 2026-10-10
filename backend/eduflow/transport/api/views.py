"""Transport endpoints. ``transport.read`` (families: their children's routes and trips; drivers: the routes
they drive), ``transport.manage`` (the office, school-wide) and ``transport.operate`` (run trips: drivers for
their routes, the office for all)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
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
from eduflow.core.api import TENANT_HEADER, errors

from .. import policies, services
from ..models import Direction, Maintenance, Rider, Route, Trip, TripStatus, Vehicle
from . import serializers as s

TAG = "transport"
READ, MANAGE, OPERATE = "transport.read", "transport.manage", "transport.operate"


class _Vehicle(ResourceView):
    tag = TAG
    resource = policies.vehicles
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.VehicleOut


@document_resource
class VehicleList(_Vehicle, ResourceListView):
    create_serializer = s.VehicleIn

    def perform_create(self, data: dict[str, Any]) -> Vehicle:
        return services.create_vehicle(self.actor, **data)


@document_resource
class VehicleDetail(_Vehicle, ResourceDetailView):
    update_serializer = s.VehicleUpdateIn

    def perform_update(self, obj: Vehicle, data: dict[str, Any]) -> Vehicle:
        return services.update_vehicle(self.actor, obj, **data)


class _Route(ResourceView):
    tag = TAG
    resource = policies.routes
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.RouteOut

    def base_queryset(self) -> QuerySet[Route]:
        return Route.objects.select_related("vehicle", "driver__membership__user").prefetch_related("stops")


@document_resource
class RouteList(_Route, ResourceListView):
    create_serializer = s.RouteIn

    def perform_create(self, data: dict[str, Any]) -> Route:
        return services.create_route(self.actor, **data)


@document_resource
class RouteDetail(_Route, ResourceDetailView):
    update_serializer = s.RouteUpdateIn

    def perform_update(self, obj: Route, data: dict[str, Any]) -> Route:
        return services.update_route(self.actor, obj, **data)


class _Rider(ResourceView):
    tag = TAG
    resource = policies.riders
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.RiderOut

    def base_queryset(self) -> QuerySet[Rider]:
        return Rider.objects.select_related("student", "stop")


@document_resource
class RiderList(_Rider, ResourceListView):
    create_serializer = s.RiderIn
    filters = [
        Filter("route_id", "route_id", serializers.UUIDField()),
        Filter("student_id", "student_id", serializers.UUIDField()),
        Filter("is_active", "is_active", serializers.BooleanField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Rider:
        return services.assign_rider(self.actor, **data)


@document_resource
class RiderDetail(_Rider, ResourceDetailView):
    allow_delete = True

    def perform_delete(self, obj: Rider) -> None:
        services.end_ride(self.actor, obj)


class _Trip(ResourceView):
    tag = TAG
    resource = policies.trips
    read_permission = READ
    output_serializer = s.TripOut

    def base_queryset(self) -> QuerySet[Trip]:
        return Trip.objects.select_related("route")


@document_resource
class TripList(_Trip, ResourceListView):
    filters = [
        Filter("route_id", "route_id", serializers.UUIDField()),
        Filter("date", "date", serializers.DateField()),
        Filter("status", "status", serializers.ChoiceField(TripStatus.choices)),
        Filter("direction", "direction", serializers.ChoiceField(Direction.choices)),
    ]


@document_resource
class TripDetail(_Trip, ResourceDetailView):
    pass


def _trip_out(trip: Trip) -> dict[str, Any]:
    return dict(s.TripOut(Trip.objects.select_related("route").get(pk=trip.pk)).data)


class RouteStartTripView(TenantAPIView):
    required_permissions = {"POST": OPERATE}

    @extend_schema(
        tags=[TAG],
        summary="Start today's trip on a route",
        parameters=[TENANT_HEADER],
        request=s.StartIn,
        responses={200: s.TripOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        route = policies.routes.get(self.actor, OPERATE, pk)
        body = s.StartIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(_trip_out(services.start_trip(self.actor, route, **body.validated_data)))


class TripArriveView(TenantAPIView):
    required_permissions = {"POST": OPERATE}

    @extend_schema(
        tags=[TAG],
        summary="Mark a trip as arrived",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.TripOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return Response(_trip_out(services.end_trip(self.actor, policies.trips.get(self.actor, OPERATE, pk))))


class TripDelayView(TenantAPIView):
    required_permissions = {"POST": OPERATE}

    @extend_schema(
        tags=[TAG],
        summary="Report a delay",
        description=f"At {services.DELAY_ALERT_MINUTES} minutes or more, the riders' families are notified.",
        parameters=[TENANT_HEADER],
        request=s.DelayIn,
        responses={200: s.TripOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        trip = policies.trips.get(self.actor, OPERATE, pk)
        body = s.DelayIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(_trip_out(services.report_delay(self.actor, trip, **body.validated_data)))


class TripPositionView(TenantAPIView):
    required_permissions = {"POST": OPERATE}

    @extend_schema(
        tags=[TAG],
        summary="Report a GPS fix for a trip on route",
        description=(
            "Sent by the driver's device or a tracking integration. EduFlow stores reported fixes only; it "
            "never estimates a position."
        ),
        parameters=[TENANT_HEADER],
        request=s.PositionIn,
        responses={201: s.PositionOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        trip = policies.trips.get(self.actor, OPERATE, pk)
        body = s.PositionIn(data=request.data)
        body.is_valid(raise_exception=True)
        position = services.report_position(self.actor, trip, **body.validated_data)
        return Response(s.PositionOut(position).data, status=201)


class _Maintenance(ResourceView):
    tag = TAG
    resource = policies.maintenance
    read_permission, write_permission = MANAGE, MANAGE
    output_serializer = s.MaintenanceOut


@document_resource
class MaintenanceList(_Maintenance, ResourceListView):
    create_serializer = s.MaintenanceIn
    filters = [Filter("vehicle_id", "vehicle_id", serializers.UUIDField())]

    def perform_create(self, data: dict[str, Any]) -> Maintenance:
        return services.log_maintenance(self.actor, **data)


@document_resource
class MaintenanceDetail(_Maintenance, ResourceDetailView):
    pass
