"""School transport (prototype ``Bus`` and ``BusStop``; screen documentation "Transport", "Live bus";
monitoring rule "Bus delayed": 10 minutes or more).

* A **vehicle** has its registration, capacity and compliance dates (insurance, fitness).
* A **route** uses a vehicle and a driver (a staff member) and has ordered **stops** with scheduled times.
  Stop coordinates are optional and only ever the school's own entries.
* A student **rides** a route from a stop (one active assignment per student).
* A **trip** is one run of a route on a day (morning or afternoon): not started -> on route -> arrived.
  The driver (or the transport office) reports a delay in minutes; families of the riders are notified.
* A **position** is a GPS fix *reported* for a trip by the driver's device or a tracking integration. EduFlow
  never estimates or simulates positions: without reports there is no live location.
* **Maintenance** records services and repairs of a vehicle, with cost and the next due date.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.core.ids import uuid7
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _uniq(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"transport_{model}_id_school_uniq")


class Vehicle(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    registration_number = models.CharField(max_length=20)
    label = models.CharField(max_length=40, blank=True, help_text='What families call it, e.g. "Bus 17".')
    capacity = models.PositiveSmallIntegerField()
    insurance_expires_on = models.DateField(null=True, blank=True)
    fitness_expires_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "transport_vehicle"
        constraints = [
            models.UniqueConstraint(
                fields=["school", "registration_number"], name="transport_vehicle_reg_uniq"
            ),
            models.CheckConstraint(condition=Q(capacity__gte=1), name="transport_vehicle_min_capacity_check"),
            _uniq("vehicle"),
        ]


class Route(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=32)
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.PROTECT, null=True, blank=True, related_name="routes"
    )
    driver = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    is_active = models.BooleanField(default=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "transport_route"
        constraints = [
            models.UniqueConstraint(fields=["school", "code"], name="transport_route_code_uniq"),
            _uniq("route"),
        ]


class Stop(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="stops")
    name = models.CharField(max_length=100)
    sequence = models.PositiveSmallIntegerField()
    pickup_time = models.TimeField(null=True, blank=True)
    drop_time = models.TimeField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "transport_stop"
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["route", "sequence"], name="transport_stop_sequence_uniq"),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180),
                name="transport_stop_coordinates_check",
            ),
            _uniq("stop"),
        ]


class Rider(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="transport")
    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="riders")
    stop = models.ForeignKey(Stop, on_delete=models.PROTECT, related_name="riders")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "transport_rider"
        constraints = [
            models.UniqueConstraint(
                fields=["student"], condition=Q(is_active=True), name="transport_one_active_route_per_student"
            ),
            _uniq("rider"),
        ]


class Direction(models.TextChoices):
    MORNING = "morning", "Morning (to school)"
    AFTERNOON = "afternoon", "Afternoon (home)"


class TripStatus(models.TextChoices):
    NOT_STARTED = "not_started", "Not started"
    ON_ROUTE = "on_route", "On route"
    ARRIVED = "arrived", "Arrived"


class Trip(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="trips")
    date = models.DateField()
    direction = models.CharField(max_length=16, choices=Direction.choices)
    status = models.CharField(max_length=16, choices=TripStatus.choices, default=TripStatus.NOT_STARTED)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    delay_minutes = models.PositiveSmallIntegerField(default=0)
    delay_reason = models.CharField(max_length=200, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "transport_trip"
        constraints = [
            models.UniqueConstraint(fields=["route", "date", "direction"], name="transport_trip_uniq"),
            models.CheckConstraint(
                condition=Q(status=TripStatus.NOT_STARTED) | Q(started_at__isnull=False),
                name="transport_trip_started_check",
            ),
            _uniq("trip"),
        ]


class Position(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="positions")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    speed_kmh = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    recorded_at = models.DateTimeField(help_text="When the device took the fix.")
    received_at = models.DateTimeField(auto_now_add=True)
    reported_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "transport_position"
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180),
                name="transport_position_coordinates_check",
            ),
            _uniq("position"),
        ]
        indexes = [models.Index(fields=["trip", "-recorded_at"], name="transport_position_latest_idx")]


class Maintenance(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="maintenance")
    date = models.DateField()
    kind = models.CharField(max_length=60)
    description = models.CharField(max_length=1000, blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    odometer_km = models.PositiveIntegerField(null=True, blank=True)
    next_due_on = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "transport_maintenance"
        constraints = [
            models.CheckConstraint(
                condition=Q(cost__isnull=True) | Q(cost__gte=0), name="transport_cost_check"
            ),
            _uniq("maintenance"),
        ]
