from datetime import time

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import SchoolScopedModel


class Direction(models.TextChoices):
    PICKUP = "pickup", "Morning pickup"
    DROP = "drop", "Afternoon drop"


class Vehicle(SchoolScopedModel):
    registration_no = models.CharField(max_length=20)
    label = models.CharField(max_length=40)
    capacity = models.PositiveSmallIntegerField(default=40)
    # Traccar "uniqueId" (usually the tracker's IMEI). Unique across all schools because
    # device data arrives before we know which school it belongs to.
    gps_device_id = models.CharField(max_length=40, null=True, blank=True, unique=True)
    last_lat = models.FloatField(null=True, blank=True)
    last_lng = models.FloatField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    # Compliance (Motor Vehicles Rule 125H / CBSE / state school-bus rules)
    has_gps_tracker = models.BooleanField(default=False)
    has_panic_button = models.BooleanField(default=False)
    has_cctv = models.BooleanField(default=False)
    has_speed_governor = models.BooleanField(default=False)
    fitness_valid_until = models.DateField(null=True, blank=True)
    permit_valid_until = models.DateField(null=True, blank=True)
    insurance_valid_until = models.DateField(null=True, blank=True)
    puc_valid_until = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return f"{self.label} ({self.registration_no})"


class Route(SchoolScopedModel):
    code = models.CharField(max_length=12)
    name = models.CharField(max_length=80)
    path = models.JSONField(default=list, help_text="[[lat, lng], ...] from the depot to the school gate")
    length_m = models.FloatField(default=0)
    avg_speed_kmh = models.FloatField(default=22)
    vehicle = models.ForeignKey(Vehicle, null=True, blank=True, on_delete=models.SET_NULL, related_name="routes")
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    attendant = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    pickup_start = models.TimeField(default=time(7, 10))
    drop_start = models.TimeField(default=time(15, 40))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="uniq_route_code")]

    def __str__(self):
        return self.name


class Stop(SchoolScopedModel):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="stops")
    name = models.CharField(max_length=80)
    lat = models.FloatField()
    lng = models.FloatField()
    sequence = models.PositiveSmallIntegerField(help_text="Order on the morning pickup")
    radius_m = models.PositiveSmallIntegerField(default=75)
    pickup_offset_min = models.PositiveSmallIntegerField(default=0)
    drop_offset_min = models.PositiveSmallIntegerField(default=0)
    is_school = models.BooleanField(default=False)

    class Meta:
        ordering = ["route", "sequence"]
        constraints = [models.UniqueConstraint(fields=["route", "sequence"], name="uniq_stop_sequence")]

    def __str__(self):
        return self.name


class StudentTransport(SchoolScopedModel):
    student = models.OneToOneField("academics.Student", on_delete=models.CASCADE, related_name="transport")
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="riders")
    pickup_stop = models.ForeignKey(Stop, on_delete=models.RESTRICT, related_name="+")
    drop_stop = models.ForeignKey(Stop, on_delete=models.RESTRICT, related_name="+")
    is_active = models.BooleanField(default=True)


class Trip(SchoolScopedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        ACTIVE = "active", "On the way"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="trips")
    vehicle = models.ForeignKey(Vehicle, null=True, blank=True, on_delete=models.SET_NULL, related_name="trips")
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    attendant = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    direction = models.CharField(max_length=8, choices=Direction.choices)
    service_date = models.DateField()
    scheduled_start = models.TimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    last_position_at = models.DateTimeField(null=True, blank=True)
    state = models.JSONField(default=dict, blank=True)  # engine.TripState
    empty_check_confirmed_at = models.DateTimeField(null=True, blank=True)
    auto_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ["service_date", "scheduled_start"]
        constraints = [
            models.UniqueConstraint(fields=["route", "direction", "service_date"], name="uniq_trip_per_day")
        ]
        indexes = [models.Index(fields=["school", "service_date", "status"], name="trip_day_status_idx")]

    def __str__(self):
        return f"{self.route.code} {self.direction} {self.service_date}"


class TripPosition(SchoolScopedModel):
    """Raw GPS history. High volume: partition by month and purge after the retention period."""

    class Source(models.TextChoices):
        DRIVER_APP = "driver_app", "Driver app"
        DEVICE = "device", "GPS device"
        SIMULATOR = "simulator", "Simulator"

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="positions")
    lat = models.FloatField()
    lng = models.FloatField()
    speed_mps = models.FloatField(null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    accuracy_m = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField()
    source = models.CharField(max_length=12, choices=Source.choices)
    client_id = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["trip", "recorded_at"], name="trip_position_time_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["trip", "client_id"], condition=Q(client_id__isnull=False), name="uniq_position_client_id"
            )
        ]


class TripStopEvent(SchoolScopedModel):
    class Kind(models.TextChoices):
        ARRIVED = "arrived", "Arrived"
        DEPARTED = "departed", "Departed"
        SKIPPED = "skipped", "Skipped"

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="stop_events")
    stop = models.ForeignKey(Stop, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["trip", "stop", "kind"], name="uniq_trip_stop_event")]


class BoardingEvent(SchoolScopedModel):
    class Kind(models.TextChoices):
        BOARDED = "boarded", "Boarded"
        DROPPED = "dropped", "Dropped off"
        NO_SHOW = "no_show", "Not at the stop"

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="boarding_events")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    at = models.DateTimeField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    client_id = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["trip", "student", "kind"], name="uniq_boarding_event")
        ]


class TransportAbsence(SchoolScopedModel):
    """A family's "not travelling today", so the bus doesn't wait and no alerts are sent."""

    class Scope(models.TextChoices):
        PICKUP = "pickup", "Morning"
        DROP = "drop", "Afternoon"
        BOTH = "both", "Both"

    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    service_date = models.DateField()
    direction = models.CharField(max_length=8, choices=Scope.choices, default=Scope.BOTH)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student", "service_date", "direction"], name="uniq_transport_absence")
        ]


class TransportPreference(SchoolScopedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    alert_minutes = models.PositiveSmallIntegerField(default=10)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "student"], name="uniq_transport_pref")]


class TripIncident(SchoolScopedModel):
    class Kind(models.TextChoices):
        SOS = "sos", "SOS"
        SPEEDING = "speeding", "Speeding"
        OFF_ROUTE = "off_route", "Off route"
        STOP_SKIPPED = "stop_skipped", "Stop skipped"
        SIGNAL_LOST = "signal_lost", "GPS signal lost"
        EMPTY_CHECK_MISSING = "empty_check_missing", "Bus-empty check not confirmed"
        # The transport desk's note on why a bus is behind (details: {"reason": "Traffic at the ORR junction"}).
        DELAY = "delay", "Running late"

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="incidents")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    at = models.DateTimeField()
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-at"]


class TripViewLog(SchoolScopedModel):
    """Who looked at a live location (children's location is sensitive; this is auditable)."""

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    first_viewed_at = models.DateTimeField()
    last_viewed_at = models.DateTimeField()
    views = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["trip", "user"], name="uniq_trip_view_log")]


class TransportException(SchoolScopedModel):
    """A pickup or drop that didn't go to plan, for the transport desk's list of the day."""

    class Kind(models.TextChoices):
        HELD = "held", "Held at pickup"
        STOP_CHANGE = "stop_change", "Stop change"
        NOT_SCANNED = "not_scanned", "Not scanned"
        OFF_MANIFEST = "off_manifest", "Off manifest"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    kind = models.CharField(max_length=14, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    # One student, or several (e.g. two riders who boarded without an ID scan).
    students = models.ManyToManyField("academics.Student", blank=True, related_name="+")
    route = models.ForeignKey(Route, null=True, blank=True, on_delete=models.CASCADE, related_name="exceptions")
    trip = models.ForeignKey(Trip, null=True, blank=True, on_delete=models.SET_NULL, related_name="exceptions")
    # A stop change: from the usual stop to today's.
    from_stop = models.ForeignKey(Stop, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    to_stop = models.ForeignKey(Stop, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=200, blank=True)
    occurred_at = models.DateTimeField()
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["school", "occurred_at"], name="transport_exc_day_idx")]
