"""Transport writes and selectors. Transactional and audited (``transport.*``).

``transport.manage`` (school-wide) manages vehicles, routes, stops, riders and maintenance.
``transport.operate`` runs trips: a driver for the routes they drive (assigned scope), the transport office
for every route (school scope). Positions are only ever stored as reported.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy import clock, domain

from .models import Maintenance, Position, Rider, Route, Stop, Trip, TripStatus, Vehicle

DELAY_ALERT_MINUTES = 10  # prototype RULES.busDelay

# A fix more than this far in the future (device clock skew) is refused.
MAX_CLOCK_SKEW = datetime.timedelta(minutes=5)


# ------------------------------------------------------------------------------------------------ setup
@transaction.atomic
def create_vehicle(actor: Actor, **data: Any) -> Vehicle:
    vehicle = domain.save(Vehicle(school=actor.school, **data), conflict="This registration number exists.")
    domain.record("transport.vehicle.created", vehicle, registration=vehicle.registration_number)
    return vehicle


@transaction.atomic
def update_vehicle(actor: Actor, vehicle: Vehicle, **data: Any) -> Vehicle:
    changed = domain.apply_changes(vehicle, data)
    if changed:
        domain.save(vehicle, conflict="This registration number exists.")
        domain.record("transport.vehicle.updated", vehicle, fields=changed)
    return vehicle


def _route_refs(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    if "vehicle_id" in data:
        vid = data.pop("vehicle_id")
        data["vehicle"] = domain.resolve(Vehicle, actor.school, vid, "vehicle_id") if vid else None
    if "driver_id" in data:
        did = data.pop("driver_id")
        data["driver"] = (
            domain.resolve(StaffProfile, actor.school, did, "driver_id", label="staff member")
            if did
            else None
        )
    return data


def _replace_stops(route: Route, stops: list[dict[str, Any]]) -> None:
    if route.riders.filter(is_active=True).exists():
        raise Conflict("Students ride this route; move them before changing its stops.")
    if len({s["sequence"] for s in stops}) != len(stops):
        raise ValidationError({"stops": ["Stop sequence numbers must be unique."]})
    for i, s in enumerate(stops):
        if (s.get("latitude") is None) != (s.get("longitude") is None):
            raise ValidationError({f"stops[{i}].latitude": ["Give both latitude and longitude, or neither."]})
    route.stops.all().delete()
    Stop.objects.bulk_create([Stop(school_id=route.school_id, route=route, **s) for s in stops])


@transaction.atomic
def create_route(actor: Actor, *, stops: list[dict[str, Any]] | None = None, **data: Any) -> Route:
    route = domain.save(
        Route(school=actor.school, **_route_refs(actor, data)), conflict="This route code exists."
    )
    _replace_stops(route, stops or [])
    domain.record("transport.route.created", route, code=route.code, stops=len(stops or []))
    return route


@transaction.atomic
def update_route(actor: Actor, route: Route, **data: Any) -> Route:
    stops = data.pop("stops", None)
    changed = domain.apply_changes(route, _route_refs(actor, data))
    if changed:
        domain.save(route, conflict="This route code exists.")
    if stops is not None:
        _replace_stops(route, stops)
        changed.append("stops")
    if changed:
        domain.record("transport.route.updated", route, fields=changed)
    return route


@transaction.atomic
def assign_rider(actor: Actor, *, student_id: Any, route_id: Any, stop_id: Any) -> Rider:
    student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
    route = domain.resolve(Route, actor.school, route_id, "route_id", label="route")
    stop = domain.resolve(Stop, actor.school, stop_id, "stop_id", label="stop")
    if stop.route_id != route.pk:
        raise ValidationError({"stop_id": ["This stop is on another route."]})
    if route.vehicle and route.riders.filter(is_active=True).count() >= route.vehicle.capacity:
        raise Conflict(f"The vehicle on this route is full ({route.vehicle.capacity} seats).")
    Rider.objects.filter(student=student, is_active=True).update(is_active=False)
    rider = Rider.objects.create(school=actor.school, student=student, route=route, stop=stop)
    domain.record("transport.rider.assigned", rider, student=str(student.pk), route=str(route.pk))
    return rider


@transaction.atomic
def end_ride(actor: Actor, rider: Rider) -> None:
    if rider.is_active:
        rider.is_active = False
        rider.save(update_fields=["is_active"])
        domain.record("transport.rider.ended", rider, student=str(rider.student_id))


@transaction.atomic
def log_maintenance(actor: Actor, *, vehicle_id: Any, **data: Any) -> Maintenance:
    vehicle = domain.resolve(Vehicle, actor.school, vehicle_id, "vehicle_id", label="vehicle")
    record = Maintenance.objects.create(
        school=actor.school, vehicle=vehicle, recorded_by=actor.membership, **data
    )
    domain.record("transport.maintenance.logged", record, vehicle=str(vehicle.pk), kind=record.kind)
    return record


# ------------------------------------------------------------------------------------------------ trips
def _family_of_riders(route: Route) -> list[Any]:
    members: list[Any] = []
    for rider in route.riders.filter(is_active=True).select_related("student"):
        members += notifications.family_of(rider.student)
    return members


@transaction.atomic
def start_trip(actor: Actor, route: Route, *, direction: str) -> Trip:
    today = clock.today(actor.school)
    trip, _ = Trip.objects.select_for_update().get_or_create(
        school_id=actor.school.pk, route=route, date=today, direction=direction
    )
    if trip.status != TripStatus.NOT_STARTED:
        raise Conflict(f"This trip is already {trip.get_status_display().lower()}.")
    trip.status, trip.started_at = TripStatus.ON_ROUTE, timezone.now()
    trip.save()
    domain.record("transport.trip.started", trip, route=str(route.pk), direction=direction)
    return trip


@transaction.atomic
def end_trip(actor: Actor, trip: Trip) -> Trip:
    trip = Trip.objects.select_for_update().get(pk=trip.pk)
    if trip.status != TripStatus.ON_ROUTE:
        raise Conflict("Only a trip on route can arrive.")
    trip.status, trip.ended_at = TripStatus.ARRIVED, timezone.now()
    trip.save()
    domain.record("transport.trip.arrived", trip, delay_minutes=trip.delay_minutes)
    return trip


@transaction.atomic
def report_delay(actor: Actor, trip: Trip, *, minutes: int, reason: str = "") -> Trip:
    trip = Trip.objects.select_for_update(of=("self",)).select_related("route__vehicle").get(pk=trip.pk)
    if trip.status != TripStatus.ON_ROUTE:
        raise Conflict("Only a trip on route can be delayed.")
    trip.delay_minutes, trip.delay_reason = minutes, reason
    trip.save(update_fields=["delay_minutes", "delay_reason"])
    domain.record("transport.trip.delay_reported", trip, minutes=minutes)
    if minutes >= DELAY_ALERT_MINUTES:
        label = (
            trip.route.vehicle.label if trip.route.vehicle and trip.route.vehicle.label else trip.route.name
        )
        notifications.notify(
            actor.school,
            _family_of_riders(trip.route),
            kind="bus",
            title=f"{label} is running {minutes} min late",
            body=reason,
            link=("trip", trip.pk),
        )
    return trip


@transaction.atomic
def report_position(
    actor: Actor,
    trip: Trip,
    *,
    latitude: Decimal,
    longitude: Decimal,
    recorded_at: datetime.datetime,
    speed_kmh: Decimal | None = None,
) -> Position:
    if trip.status != TripStatus.ON_ROUTE:
        raise Conflict("Positions are accepted only while the trip is on route.")
    if recorded_at > timezone.now() + MAX_CLOCK_SKEW:
        raise ValidationError({"recorded_at": ["This fix is in the future."]})
    if trip.started_at and recorded_at < trip.started_at - MAX_CLOCK_SKEW:
        raise ValidationError({"recorded_at": ["This fix is from before the trip started."]})
    return Position.objects.create(
        school=actor.school,
        trip=trip,
        latitude=latitude,
        longitude=longitude,
        speed_kmh=speed_kmh,
        recorded_at=recorded_at,
        reported_by=actor.membership,
    )


def latest_position(trip: Trip) -> Position | None:
    return trip.positions.order_by("-recorded_at").first()


def delayed_trips(school: Any, day: datetime.date) -> list[Trip]:
    """Trips on route today with a reported delay at or above the alert threshold ("Bus delayed")."""
    return list(
        Trip.objects.filter(
            school_id=school.pk, date=day, status=TripStatus.ON_ROUTE, delay_minutes__gte=DELAY_ALERT_MINUTES
        ).select_related("route__vehicle", "route__driver__membership")
    )


def compliance_due(school: Any, within_days: int = 30) -> list[Vehicle]:
    """Active vehicles whose insurance or fitness expires within the window (or has expired)."""
    horizon = clock.today(school) + datetime.timedelta(days=within_days)
    return list(
        Vehicle.objects.filter(school_id=school.pk, is_active=True).filter(
            Q(insurance_expires_on__lte=horizon) | Q(fitness_expires_on__lte=horizon)
        )
    )
