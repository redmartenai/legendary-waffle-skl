from __future__ import annotations

from decimal import Decimal
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import Direction, Maintenance, Position, Rider, Route, Stop, Trip, Vehicle


def Coordinate(lo: int, **kwargs: Any) -> serializers.DecimalField:
    return serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=Decimal(-lo), max_value=Decimal(lo), **kwargs
    )


class VehicleOut(serializers.ModelSerializer[Vehicle]):
    class Meta:
        model = Vehicle
        fields = (
            "id",
            "registration_number",
            "label",
            "capacity",
            "insurance_expires_on",
            "fitness_expires_on",
            "is_active",
        )
        read_only_fields = fields


class VehicleIn(StrictSerializer):
    registration_number = serializers.CharField(max_length=20)
    label = serializers.CharField(max_length=40, required=False, allow_blank=True)  # type: ignore[assignment]
    capacity = serializers.IntegerField(min_value=1, max_value=200)
    insurance_expires_on = serializers.DateField(required=False, allow_null=True)
    fitness_expires_on = serializers.DateField(required=False, allow_null=True)


class VehicleUpdateIn(StrictSerializer):
    label = serializers.CharField(max_length=40, required=False, allow_blank=True)  # type: ignore[assignment]
    capacity = serializers.IntegerField(min_value=1, max_value=200, required=False)
    insurance_expires_on = serializers.DateField(required=False, allow_null=True)
    fitness_expires_on = serializers.DateField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)


class StopOut(serializers.ModelSerializer[Stop]):
    class Meta:
        model = Stop
        fields = ("id", "name", "sequence", "pickup_time", "drop_time", "latitude", "longitude")
        read_only_fields = fields


class StopIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    sequence = serializers.IntegerField(min_value=1, max_value=500)
    pickup_time = serializers.TimeField(required=False, allow_null=True)
    drop_time = serializers.TimeField(required=False, allow_null=True)
    latitude = Coordinate(90, required=False, allow_null=True)
    longitude = Coordinate(180, required=False, allow_null=True)


class RouteOut(serializers.ModelSerializer[Route]):
    vehicle = VehicleOut(allow_null=True)
    driver = serializers.CharField(source="driver.membership.user.full_name", allow_null=True, default=None)
    stops = StopOut(many=True)

    class Meta:
        model = Route
        fields = ("id", "name", "code", "vehicle", "driver", "is_active", "stops")
        read_only_fields = fields


class RouteIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    code = serializers.SlugField(max_length=32)
    vehicle_id = serializers.UUIDField(required=False, allow_null=True)
    driver_id = serializers.UUIDField(required=False, allow_null=True)
    stops = StopIn(many=True, required=False)


class RouteUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=100, required=False)
    vehicle_id = serializers.UUIDField(required=False, allow_null=True)
    driver_id = serializers.UUIDField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)
    stops = StopIn(many=True, required=False)


class RiderOut(serializers.ModelSerializer[Rider]):
    student = PersonRef()
    route_id = serializers.UUIDField()
    stop = StopOut()

    class Meta:
        model = Rider
        fields = ("id", "student", "route_id", "stop", "is_active", "created_at")
        read_only_fields = fields


class RiderIn(StrictSerializer):
    student_id = serializers.UUIDField()
    route_id = serializers.UUIDField()
    stop_id = serializers.UUIDField()


class PositionOut(serializers.ModelSerializer[Position]):
    class Meta:
        model = Position
        fields = ("latitude", "longitude", "speed_kmh", "recorded_at")
        read_only_fields = fields


class TripOut(serializers.ModelSerializer[Trip]):
    route_id = serializers.UUIDField()
    route = serializers.CharField(source="route.name")
    last_position = serializers.SerializerMethodField()

    class Meta:
        model = Trip
        fields = (
            "id",
            "route_id",
            "route",
            "date",
            "direction",
            "status",
            "started_at",
            "ended_at",
            "delay_minutes",
            "delay_reason",
            "last_position",
        )
        read_only_fields = fields

    @extend_schema_field(PositionOut(allow_null=True))
    def get_last_position(self, trip: Trip) -> dict[str, Any] | None:
        """Only a reported fix; there is no estimated position."""
        latest = trip.positions.order_by("-recorded_at").first()
        return dict(PositionOut(latest).data) if latest else None


class StartIn(StrictSerializer):
    direction = serializers.ChoiceField(Direction.choices)


class DelayIn(StrictSerializer):
    minutes = serializers.IntegerField(min_value=0, max_value=600)
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)


class PositionIn(StrictSerializer):
    latitude = Coordinate(90)
    longitude = Coordinate(180)
    speed_kmh = serializers.DecimalField(
        max_digits=5, decimal_places=1, min_value=Decimal(0), required=False, allow_null=True
    )
    recorded_at = serializers.DateTimeField()


class MaintenanceOut(serializers.ModelSerializer[Maintenance]):
    vehicle_id = serializers.UUIDField()

    class Meta:
        model = Maintenance
        fields = ("id", "vehicle_id", "date", "kind", "description", "cost", "odometer_km", "next_due_on")
        read_only_fields = fields


class MaintenanceIn(StrictSerializer):
    vehicle_id = serializers.UUIDField()
    date = serializers.DateField()
    kind = serializers.CharField(max_length=60)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal(0), required=False, allow_null=True
    )
    odometer_km = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    next_due_on = serializers.DateField(required=False, allow_null=True)
