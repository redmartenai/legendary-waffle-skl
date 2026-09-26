"""
Live-tracking services: trips, GPS ingestion, alerts and the live view.

Flow: driver app / GPS device / simulator -> ingest_positions() -> engine.advance()
-> stop events + alerts (deduplicated) -> Centrifugo publish (parents' map updates).
Clients also poll GET /trips/{id}/live, so tracking still works without realtime.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from apps.academics.access import guardians_of
from apps.academics.models import Student, StudentGuardian
from apps.accounts.models import CREW_ROLES, TRANSPORT_STAFF_ROLES, Membership, Role
from apps.core.tenant import unscoped, use_school
from apps.core.utils import school_today, school_tz
from apps.notifications.models import Category, Priority
from apps.notifications.services import notify
from apps.realtime import client as realtime

from . import engine
from .models import (
    BoardingEvent,
    Direction,
    Route,
    Stop,
    StudentTransport,
    TransportAbsence,
    TransportPreference,
    Trip,
    TripIncident,
    TripPosition,
    TripStopEvent,
    TripViewLog,
    Vehicle,
)

logger = logging.getLogger("apps.transport")

KNOTS_TO_MPS = 0.514444
# Stop alerts from an offline backlog older than this are recorded but not pushed to families.
STALE_ALERT_SECONDS = 300


# ---------------------------------------------------------------- route plans


@dataclass(frozen=True)
class RoutePlan:
    geometry: engine.RouteGeometry
    stops: tuple[engine.StopInfo, ...]
    stop_models: dict


_PLAN_CACHE: dict = {}


def route_plan(route: Route, direction: str) -> RoutePlan:
    """Geometry and stops in travel order. Afternoon drops run the route in reverse."""
    # Callers that prefetch "stops" (e.g. a whole fleet at once) save a query per route.
    prefetched = getattr(route, "_prefetched_objects_cache", {}).get("stops")
    stops = sorted(prefetched, key=lambda s: s.sequence) if prefetched is not None else list(Stop.objects.filter(route=route).order_by("sequence"))
    signature = (
        str(route.id),
        direction,
        route.updated_at.isoformat() if route.updated_at else "",
        tuple((str(s.id), s.updated_at.isoformat() if s.updated_at else "") for s in stops),
    )
    cached = _PLAN_CACHE.get(signature)
    if cached is not None:
        return cached

    points, ordered = list(route.path), stops
    if direction == Direction.DROP:
        points, ordered = list(reversed(points)), list(reversed(stops))
    geometry = engine.RouteGeometry(points)
    infos = tuple(
        engine.StopInfo(
            id=str(stop.id),
            name=stop.name,
            lat=stop.lat,
            lng=stop.lng,
            order=index,
            distance_m=geometry.project(stop.lat, stop.lng).distance_m,
            radius_m=float(stop.radius_m),
            offset_min=stop.pickup_offset_min if direction == Direction.PICKUP else stop.drop_offset_min,
        )
        for index, stop in enumerate(ordered)
    )
    plan = RoutePlan(geometry, infos, {str(s.id): s for s in stops})
    if len(_PLAN_CACHE) > 256:
        _PLAN_CACHE.clear()
    _PLAN_CACHE[signature] = plan
    return plan


def refresh_route_length(route: Route) -> None:
    route.length_m = engine.RouteGeometry(route.path).length_m
    route.save(update_fields=["length_m", "updated_at"])


# ---------------------------------------------------------------- trips


def ensure_trips_for_date(route: Route, service_date) -> list[Trip]:
    trips = []
    for direction, start in ((Direction.PICKUP, route.pickup_start), (Direction.DROP, route.drop_start)):
        trip, _ = Trip.objects.get_or_create(
            route=route,
            direction=direction,
            service_date=service_date,
            defaults={
                "vehicle": route.vehicle,
                "driver": route.driver,
                "attendant": route.attendant,
                "scheduled_start": start,
            },
        )
        trips.append(trip)
    return trips


def ensure_today_trips(school) -> None:
    today = school_today(school)
    have = set(Trip.objects.filter(service_date=today).values_list("route_id", "direction"))
    for route in Route.objects.filter(is_active=True):
        # Only touch routes missing a trip: one query for the whole fleet on a normal day.
        if (route.id, Direction.PICKUP) not in have or (route.id, Direction.DROP) not in have:
            ensure_trips_for_date(route, today)


@dataclass(frozen=True)
class Rider:
    student: Student
    stop_id: str
    absent: bool


def riders(trip: Trip) -> list[Rider]:
    assignments = StudentTransport.objects.filter(
        route=trip.route, is_active=True, student__is_active=True
    ).select_related("student", "student__class_group")
    absent_ids = set(
        TransportAbsence.objects.filter(
            service_date=trip.service_date,
            direction__in=[trip.direction, TransportAbsence.Scope.BOTH],
        ).values_list("student_id", flat=True)
    )
    result = []
    for assignment in assignments:
        stop_id = assignment.pickup_stop_id if trip.direction == Direction.PICKUP else assignment.drop_stop_id
        result.append(Rider(assignment.student, str(stop_id), assignment.student_id in absent_ids))
    return result


def transport_staff() -> list:
    memberships = Membership.objects.filter(role__in=TRANSPORT_STAFF_ROLES, is_active=True).select_related("user")
    return list({m.user_id: m.user for m in memberships}.values())


def _names(students) -> str:
    names = sorted({s.first_name for s in students})
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _vehicle_label(trip: Trip) -> str:
    return trip.vehicle.label if trip.vehicle else "The bus"


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=dt_timezone.utc)


def _clock(moment: datetime, school) -> str:
    return moment.astimezone(school_tz(school)).strftime("%I:%M %p").lstrip("0")


def _scheduled_at(trip: Trip, offset_min: int) -> datetime:
    start = datetime.combine(trip.service_date, trip.scheduled_start, tzinfo=school_tz(trip.school))
    return start + timedelta(minutes=offset_min)


def start_trip(trip: Trip, user) -> Trip:
    with transaction.atomic():
        trip = Trip.objects.select_for_update(of=("self",)).select_related("route", "vehicle", "school").get(pk=trip.pk)
        if trip.status == Trip.Status.ACTIVE:
            return trip  # idempotent: a double tap or a retried request
        if trip.status != Trip.Status.SCHEDULED:
            raise ValidationError({"trip": "This trip has already finished."})
        trip.status = Trip.Status.ACTIVE
        trip.started_at = timezone.now()
        trip.state = engine.TripState().to_dict()
        if trip.driver_id is None and user is not None:
            trip.driver = user
        trip.save(update_fields=["status", "started_at", "state", "driver", "updated_at"])
        plan = route_plan(trip.route, trip.direction)
        _notify_started(trip, plan)
        _publish(trip, plan)
    return trip


def end_trip(trip: Trip, user, *, empty_check_confirmed: bool) -> Trip:
    if not empty_check_confirmed:
        raise ValidationError(
            {"empty_check_confirmed": "Walk through the bus and confirm no child is left on board."}
        )
    with transaction.atomic():
        trip = Trip.objects.select_for_update(of=("self",)).select_related("route", "vehicle", "school").get(pk=trip.pk)
        if trip.status == Trip.Status.COMPLETED:
            return trip
        if trip.status != Trip.Status.ACTIVE:
            raise ValidationError({"trip": "This trip isn't running."})
        now = timezone.now()
        trip.status = Trip.Status.COMPLETED
        trip.ended_at = now
        trip.empty_check_confirmed_at = now
        trip.save(update_fields=["status", "ended_at", "empty_check_confirmed_at", "updated_at"])
        _publish(trip, route_plan(trip.route, trip.direction))
    return trip


# ---------------------------------------------------------------- ingestion


def ingest_positions(trip: Trip, fixes: list[dict], source: str) -> dict:
    """Apply a batch of GPS fixes (each: lat, lng, at [epoch s], optional speed_mps,
    heading, accuracy_m, client_id). Idempotent per client_id, safe to retry."""
    with transaction.atomic():
        trip = Trip.objects.select_for_update(of=("self",)).select_related("route", "vehicle", "school").get(pk=trip.pk)
        if trip.status != Trip.Status.ACTIVE:
            raise ValidationError({"trip": "Start the trip before sending locations."}, code="trip_not_active")
        plan = route_plan(trip.route, trip.direction)
        state = engine.TripState.from_dict(trip.state)
        speed_limit = trip.school.policy("transport", "speed_limit_kmh") or 40

        # A phone with a wrong clock must not stall tracking: fixes stamped well in the
        # future are dropped instead of blocking every later (correct) fix as "out of order".
        now = time.time()
        future = [f for f in fixes if f["at"] > now + 120]
        if future:
            logger.warning("Dropped %s future-dated fixes for trip %s (device clock skew)", len(future), trip.id)
        incoming = sorted((f for f in fixes if f["at"] <= now + 120), key=lambda f: f["at"])
        client_ids = [f["client_id"] for f in incoming if f.get("client_id")]
        seen = (
            set(TripPosition.objects.filter(trip=trip, client_id__in=client_ids).values_list("client_id", flat=True))
            if client_ids
            else set()
        )
        rows, events, accepted = [], [], 0
        for fix in incoming:
            client_id = fix.get("client_id")
            if client_id and client_id in seen:
                continue
            if client_id:
                seen.add(client_id)
            rows.append(
                TripPosition(
                    school=trip.school,
                    trip=trip,
                    lat=fix["lat"],
                    lng=fix["lng"],
                    speed_mps=fix.get("speed_mps"),
                    heading=fix.get("heading"),
                    accuracy_m=fix.get("accuracy_m"),
                    recorded_at=_utc(fix["at"]),
                    source=source,
                    client_id=client_id,
                )
            )
            state, new_events, ok = engine.advance(
                state,
                plan.geometry,
                plan.stops,
                engine.Fix(
                    lat=fix["lat"],
                    lng=fix["lng"],
                    at=fix["at"],
                    speed_mps=fix.get("speed_mps"),
                    heading=fix.get("heading"),
                    accuracy_m=fix.get("accuracy_m"),
                ),
                speed_limit_kmh=speed_limit,
            )
            if ok:
                accepted += 1
                events.extend(new_events)

        if rows:
            TripPosition.objects.bulk_create(rows, ignore_conflicts=True)
        if accepted:
            trip.state = state.to_dict()
            trip.last_position_at = _utc(state.last_fix_at)
            trip.save(update_fields=["state", "last_position_at", "updated_at"])
            if trip.vehicle_id:
                Vehicle.objects.filter(pk=trip.vehicle_id).update(
                    last_lat=state.last_lat, last_lng=state.last_lng, last_seen_at=trip.last_position_at
                )
            _record_events(trip, plan, events)
            live = live_state(trip, plan=plan)
            _notify_approaching(trip, plan, live)
            _notify_delay(trip, plan, live)
            _publish(trip, plan, live)
    return {
        "received": len(fixes),
        "stored": len(rows),
        "accepted": accepted,
        "rejected_future": len(future),
        "events": [e.kind for e in events],
    }


def ingest_traccar(payload: dict) -> dict:
    """Traccar JSON forwarding (forward.type=json): {"position": {...}, "device": {...}}."""
    device = payload.get("device") or {}
    position = payload.get("position") or {}
    unique_id = str(device.get("uniqueId") or "").strip()
    if not unique_id or "latitude" not in position or "longitude" not in position:
        raise ValidationError({"payload": "Expected Traccar position and device objects."})
    if position.get("valid") is False:
        return {"ignored": "invalid_fix"}

    with unscoped():
        vehicle = Vehicle.all_objects.select_related("school").filter(gps_device_id=unique_id, is_active=True).first()
    if vehicle is None:
        return {"ignored": "unknown_device"}

    fixed_at = parse_datetime(str(position.get("fixTime") or position.get("deviceTime") or "")) or timezone.now()
    accuracy = position.get("accuracy") or None  # Traccar sends 0 when unknown
    fix = {
        "lat": float(position["latitude"]),
        "lng": float(position["longitude"]),
        "at": fixed_at.timestamp(),
        "speed_mps": float(position.get("speed") or 0) * KNOTS_TO_MPS,
        "heading": position.get("course"),
        "accuracy_m": float(accuracy) if accuracy else None,
        "client_id": f"traccar:{position['id']}" if position.get("id") is not None else None,
    }
    with use_school(vehicle.school):
        trip = Trip.objects.filter(vehicle=vehicle, status=Trip.Status.ACTIVE).order_by("-started_at").first()
        if trip is None:
            Vehicle.objects.filter(pk=vehicle.pk).update(last_lat=fix["lat"], last_lng=fix["lng"], last_seen_at=fixed_at)
            return {"stored": 0, "note": "no_active_trip"}
        return ingest_positions(trip, [fix], TripPosition.Source.DEVICE)


# ---------------------------------------------------------------- events & alerts


def _record_events(trip: Trip, plan: RoutePlan, events: list[engine.Event]) -> None:
    for event in events:
        stop = plan.stop_models.get(event.stop_id) if event.stop_id else None
        at = _utc(event.at)
        if event.kind in {"arrived", "departed", "skipped"} and stop is not None:
            TripStopEvent.objects.get_or_create(trip=trip, stop=stop, kind=event.kind, defaults={"at": at})
        fresh = time.time() - event.at <= STALE_ALERT_SECONDS
        if event.kind == "arrived" and stop is not None and fresh:
            _notify_stop_arrival(trip, stop, at)
        elif event.kind == "final_arrived" and trip.direction == Direction.PICKUP and fresh:
            _notify_reached_school(trip, at)
        elif event.kind == "skipped" and stop is not None:
            if any(r.stop_id == str(stop.id) and not r.absent for r in riders(trip)):
                _incident(trip, TripIncident.Kind.STOP_SKIPPED, at, {"stop": stop.name}, f"Skipped {stop.name}")
        elif event.kind == "off_route":
            _incident(trip, TripIncident.Kind.OFF_ROUTE, at, {"offset_m": event.value}, "Bus left its route")
        elif event.kind == "speeding":
            bucket = int(event.at // 300)  # at most one speeding alert per 5 minutes
            _incident(
                trip,
                TripIncident.Kind.SPEEDING,
                at,
                {"speed_kmh": event.value},
                f"Speeding at {event.value:.0f} km/h",
                dedupe=f"bus:{trip.id}:speeding:{bucket}",
            )


def _incident(trip: Trip, kind: str, at: datetime, details: dict, headline: str, dedupe: str | None = None):
    dedupe = dedupe or f"bus:{trip.id}:{kind}"
    state = engine.TripState.from_dict(trip.state)
    created = notify(
        transport_staff(),
        school=trip.school,
        category=Category.SAFETY,
        title=f"{_vehicle_label(trip)}: {headline}",
        body=f"{trip.route.name} · {_clock(at, trip.school)}",
        data={"trip_id": str(trip.id), "type": kind},
        dedupe_key=dedupe,
        priority=Priority.HIGH,
    )
    if created:
        TripIncident.objects.create(trip=trip, kind=kind, at=at, lat=state.last_lat, lng=state.last_lng, details=details)


def _guardians_by_stop(active: list[Rider]) -> list[tuple]:
    """(guardian, stop_id, [students]) with siblings at the same stop merged."""
    stop_of = {r.student.id: r.stop_id for r in active}
    grouped = []
    for user, students in guardians_of([r.student for r in active]).items():
        by_stop: dict = {}
        for student in students:
            by_stop.setdefault(stop_of[student.id], []).append(student)
        grouped.extend((user, stop_id, kids) for stop_id, kids in by_stop.items())
    return grouped


def _notify_started(trip: Trip, plan: RoutePlan) -> None:
    active = [r for r in riders(trip) if not r.absent]
    for user, stop_id, kids in _guardians_by_stop(active):
        stop = plan.stop_models[stop_id]
        notify(
            [user],
            school=trip.school,
            category=Category.BUS,
            title=f"{_vehicle_label(trip)} is on the way",
            body=f"{trip.route.name} has started. We'll alert you before it reaches {stop.name} ({_names(kids)}).",
            data={"trip_id": str(trip.id), "type": "trip_started"},
            dedupe_key=f"bus:{trip.id}:started",
        )


def _notify_stop_arrival(trip: Trip, stop: Stop, at: datetime) -> None:
    active = [r for r in riders(trip) if not r.absent and r.stop_id == str(stop.id)]
    for user, _, kids in _guardians_by_stop(active):
        body = (
            f"{_vehicle_label(trip)} is at {stop.name} now ({_names(kids)})."
            if trip.direction == Direction.PICKUP
            else f"{_vehicle_label(trip)} reached {stop.name}. {_names(kids)} will get off here."
        )
        notify(
            [user],
            school=trip.school,
            category=Category.BUS,
            title=f"Bus at {stop.name}",
            body=body,
            data={"trip_id": str(trip.id), "stop_id": str(stop.id), "type": "arrived"},
            dedupe_key=f"bus:{trip.id}:{stop.id}:arrived",
            priority=Priority.HIGH,
        )


def _notify_reached_school(trip: Trip, at: datetime) -> None:
    active = [r for r in riders(trip) if not r.absent]
    for user, _, kids in _guardians_by_stop(active):
        notify(
            [user],
            school=trip.school,
            category=Category.BUS,
            title="Reached school",
            body=f"{_vehicle_label(trip)} reached school at {_clock(at, trip.school)} ({_names(kids)}).",
            data={"trip_id": str(trip.id), "type": "reached_school"},
            dedupe_key=f"bus:{trip.id}:reached",
        )


def _notify_approaching(trip: Trip, plan: RoutePlan, live: dict) -> None:
    if live["signal"] == "lost":
        return  # an old position can't tell anyone the bus is close now
    etas = {
        s["id"]: s["eta_seconds"]
        for s in live["stops"]
        if s["status"] in {"next", "upcoming"} and s["eta_seconds"] is not None
    }
    if not etas:
        return
    active = [r for r in riders(trip) if not r.absent and r.stop_id in etas]
    if not active:
        return
    default_minutes = trip.school.policy("transport", "default_alert_minutes") or 10
    prefs = {
        (p.user_id, p.student_id): p.alert_minutes
        for p in TransportPreference.objects.filter(student__in=[r.student for r in active])
    }
    for user, stop_id, kids in _guardians_by_stop(active):
        threshold_min = max(prefs.get((user.id, kid.id), default_minutes) for kid in kids)
        if threshold_min == 0:
            continue  # this family turned the approach alert off
        eta = etas[stop_id]
        if eta > threshold_min * 60:
            continue
        minutes = max(1, round(eta / 60))
        stop = plan.stop_models[stop_id]
        notify(
            [user],
            school=trip.school,
            category=Category.BUS,
            title=f"Bus is about {minutes} min away",
            body=f"{_vehicle_label(trip)} will reach {stop.name} in about {minutes} min ({_names(kids)}).",
            data={"trip_id": str(trip.id), "stop_id": stop_id, "type": "approaching"},
            dedupe_key=f"bus:{trip.id}:{stop_id}:approach",
            priority=Priority.HIGH,
        )


def _notify_delay(trip: Trip, plan: RoutePlan, live: dict) -> None:
    delay = live.get("delay_minutes")
    # Only plausible delays: a huge number means a schedule problem, not a late bus.
    if delay is None or not 10 <= delay <= 120:
        return
    upcoming = {s["id"] for s in live["stops"] if s["status"] in {"next", "upcoming"}}
    active = [r for r in riders(trip) if not r.absent and r.stop_id in upcoming]
    for user, _, kids in _guardians_by_stop(active):
        notify(
            [user],
            school=trip.school,
            category=Category.BUS,
            title="Bus running late",
            body=f"{_vehicle_label(trip)} is running about {delay} min late today ({_names(kids)}).",
            data={"trip_id": str(trip.id), "type": "delay"},
            dedupe_key=f"bus:{trip.id}:delay",
        )


def record_boarding(trip: Trip, student: Student, kind: str, user, client_id: str | None = None) -> BoardingEvent:
    if trip.status != Trip.Status.ACTIVE:
        raise ValidationError({"trip": "Start the trip first."})
    rider = next((r for r in riders(trip) if r.student.id == student.id), None)
    if rider is None:
        raise ValidationError({"student": "This student isn't on this route."})
    event, created = BoardingEvent.objects.get_or_create(
        trip=trip,
        student=student,
        kind=kind,
        defaults={"at": timezone.now(), "recorded_by": user, "client_id": client_id},
    )
    if created:
        stop = Stop.objects.get(pk=rider.stop_id)
        clock = _clock(event.at, trip.school)
        titles = {
            BoardingEvent.Kind.BOARDED: (f"{student.first_name} boarded the bus", f"Boarded at {stop.name}, {clock}."),
            BoardingEvent.Kind.DROPPED: (f"{student.first_name} got off the bus", f"Dropped at {stop.name}, {clock}."),
            BoardingEvent.Kind.NO_SHOW: (
                f"{student.first_name} wasn't at the stop",
                f"The bus waited at {stop.name} and has moved on. Message the transport desk if this is a mistake.",
            ),
        }
        title, body = titles[kind]
        notify(
            list(guardians_of([student]).keys()),
            school=trip.school,
            category=Category.BUS,
            title=title,
            body=body,
            data={"trip_id": str(trip.id), "student_id": str(student.id), "type": f"boarding_{kind}"},
            dedupe_key=f"bus:{trip.id}:{student.id}:{kind}",
            priority=Priority.HIGH if kind == BoardingEvent.Kind.NO_SHOW else Priority.NORMAL,
        )
    return event


def trigger_sos(trip: Trip, user, lat: float | None = None, lng: float | None = None, note: str = "") -> TripIncident:
    now = timezone.now()
    incident = TripIncident.objects.create(
        trip=trip,
        kind=TripIncident.Kind.SOS,
        at=now,
        lat=lat,
        lng=lng,
        details={"note": note[:300], "raised_by": str(user.id)},
    )
    notify(
        transport_staff(),
        school=trip.school,
        category=Category.SAFETY,
        title=f"SOS from {_vehicle_label(trip)}",
        body=f"{user.full_name} raised an SOS on {trip.route.name} at {_clock(now, trip.school)}. {note}".strip(),
        data={"trip_id": str(trip.id), "incident_id": str(incident.id), "lat": lat, "lng": lng, "type": "sos"},
        priority=Priority.CRITICAL,
    )
    return incident


# ---------------------------------------------------------------- live view


def live_state(trip: Trip, *, plan: RoutePlan | None = None, now: float | None = None, staff: bool = False) -> dict:
    plan = plan or route_plan(trip.route, trip.direction)
    now = now or time.time()
    state = engine.TripState.from_dict(trip.state)
    active = trip.status == Trip.Status.ACTIVE
    tz = school_tz(trip.school)
    has_fix = active and state.last_fix_at is not None
    etas = engine.stop_etas(state, plan.stops, trip.route.avg_speed_kmh / 3.6) if has_fix else {}
    # Clamp: fixes can carry timestamps slightly ahead of the server clock.
    age = max(0.0, now - state.last_fix_at) if state.last_fix_at is not None else None
    signal = engine.signal_status(state, now) if active else "none"

    stops, next_stop, next_marked = [], None, False
    for info in plan.stops:
        arrived = state.arrived.get(info.id)
        departed = state.departed.get(info.id)
        if info.id in state.skipped:
            status = "skipped"
        elif arrived and not departed:
            status = "at_stop"
        elif arrived:
            status = "departed"
        elif active and not next_marked:
            status = "next"
        else:
            status = "upcoming"
        if status in {"at_stop", "next"}:
            next_marked = True

        eta = etas.get(info.id)
        if eta is not None and age is not None and signal != "lost":
            eta = max(0, int(eta - age))
        if status == "at_stop":
            eta = 0
        item = {
            "id": info.id,
            "name": info.name,
            "lat": info.lat,
            "lng": info.lng,
            "order": info.order,
            "status": status,
            "eta_seconds": eta if status in {"next", "upcoming", "at_stop"} else None,
            "scheduled_time": _scheduled_at(trip, info.offset_min).astimezone(tz).strftime("%H:%M"),
            "arrived_at": _utc(arrived).isoformat() if arrived else None,
        }
        stops.append(item)
        if next_stop is None and status in {"next", "at_stop"}:
            next_stop = item

    delay_minutes = None
    if has_fix and plan.stops:
        final = plan.stops[-1]
        final_eta = etas.get(final.id)
        if final_eta is not None:
            predicted = state.last_fix_at + final_eta
            scheduled_final = _scheduled_at(trip, final.offset_min).timestamp()
            delay_minutes = max(0, round((predicted - scheduled_final) / 60))

    share = trip.school.policy("transport", "share_live_location_with_parents")
    position = None
    if has_fix and (staff or share):
        position = {
            "lat": state.last_lat,
            "lng": state.last_lng,
            "heading": None if state.heading is None else round(state.heading),
            "speed_kmh": state.last_speed_kmh,
            "recorded_at": _utc(state.last_fix_at).isoformat(),
        }

    payload = {
        "trip_id": str(trip.id),
        "status": trip.status,
        "direction": trip.direction,
        "service_date": trip.service_date.isoformat(),
        "scheduled_start": trip.scheduled_start.strftime("%H:%M"),
        "route": {"id": str(trip.route_id), "code": trip.route.code, "name": trip.route.name},
        "vehicle": {"label": trip.vehicle.label, "registration_no": trip.vehicle.registration_no}
        if trip.vehicle
        else None,
        "crew": {
            "driver": trip.driver.first_name if trip.driver else None,
            "attendant": trip.attendant.first_name if trip.attendant else None,
        },
        "started_at": trip.started_at.isoformat() if trip.started_at else None,
        "ended_at": trip.ended_at.isoformat() if trip.ended_at else None,
        "position": position,
        "signal": signal,
        "last_update_seconds": None if age is None or not active else int(age),
        "progress": {"distance_m": round(state.progress_m), "total_m": round(plan.geometry.length_m)},
        "next_stop": next_stop,
        "stops": stops,
        "delay_minutes": delay_minutes,
        "arrived_at_school": _utc(state.final_arrived_at).isoformat()
        if state.final_arrived_at and trip.direction == Direction.PICKUP
        else None,
        "generated_at": _utc(now).isoformat(),
    }
    if staff:
        payload["staff"] = {
            "offset_m": round(state.offset_m),
            "off_route": state.off_route_streak >= engine.OFF_ROUTE_STREAK,
            "empty_check_confirmed_at": trip.empty_check_confirmed_at.isoformat()
            if trip.empty_check_confirmed_at
            else None,
            "auto_closed": trip.auto_closed,
        }
    return payload


def _publish(trip: Trip, plan: RoutePlan, live: dict | None = None) -> None:
    payload = live or live_state(trip, plan=plan)
    realtime.publish(realtime.trip_channel(trip.id), {"type": "trip.live", "trip": payload})


# ---------------------------------------------------------------- access


def trip_access(request, trip: Trip) -> str | None:
    """'staff', 'family' or None. Families only see trips for their own children's route."""
    roles, user = request.roles, request.user
    if roles & TRANSPORT_STAFF_ROLES:
        return "staff"
    if roles & CREW_ROLES and user.id in {trip.driver_id, trip.attendant_id}:
        return "staff"
    student_ids: set = set()
    if Role.PARENT in roles:
        student_ids |= set(StudentGuardian.objects.filter(user=user).values_list("student_id", flat=True))
    if Role.STUDENT in roles:
        student_ids |= set(Student.objects.filter(user=user).values_list("id", flat=True))
    if student_ids and StudentTransport.objects.filter(
        route=trip.route, student_id__in=student_ids, is_active=True
    ).exists():
        return "family"
    return None


def log_view(trip: Trip, user) -> None:
    now = timezone.now()
    log = TripViewLog.objects.filter(trip=trip, user=user).first()
    if log is None:
        TripViewLog.objects.create(trip=trip, user=user, first_viewed_at=now, last_viewed_at=now)
    elif (now - log.last_viewed_at).total_seconds() > 60:
        TripViewLog.objects.filter(pk=log.pk).update(last_viewed_at=now, views=log.views + 1)


# ---------------------------------------------------------------- monitoring


def monitor_active_trips(now: float | None = None) -> dict:
    """Run every minute (cron / scheduler): lost-signal alerts and auto-closing forgotten trips."""
    now = now or time.time()
    summary = {"checked": 0, "signal_alerts": 0, "auto_closed": 0}
    with unscoped():
        trips = list(Trip.all_objects.filter(status=Trip.Status.ACTIVE).select_related("school", "route", "vehicle"))
    for trip in trips:
        with use_school(trip.school):
            summary["checked"] += 1
            state = engine.TripState.from_dict(trip.state)
            started = trip.started_at.timestamp() if trip.started_at else now
            no_fix_yet = state.last_fix_at is None and now - started > 300
            if engine.signal_status(state, now) == "lost" or no_fix_yet:
                created = notify(
                    transport_staff(),
                    school=trip.school,
                    category=Category.SAFETY,
                    title=f"{_vehicle_label(trip)}: GPS signal lost",
                    body=f"No location from {trip.route.name} for a while. Call the crew to check.",
                    data={"trip_id": str(trip.id), "type": "signal_lost"},
                    dedupe_key=f"bus:{trip.id}:signal:{int(now // 600)}",
                    priority=Priority.HIGH,
                )
                if created:
                    summary["signal_alerts"] += 1
                    TripIncident.objects.create(trip=trip, kind=TripIncident.Kind.SIGNAL_LOST, at=_utc(now))
            forgotten = (state.final_arrived_at and now - state.final_arrived_at > 1800) or (now - started > 4 * 3600)
            if forgotten:
                trip.status = Trip.Status.COMPLETED
                trip.ended_at = _utc(now)
                trip.auto_closed = True
                trip.save(update_fields=["status", "ended_at", "auto_closed", "updated_at"])
                TripIncident.objects.create(trip=trip, kind=TripIncident.Kind.EMPTY_CHECK_MISSING, at=_utc(now))
                notify(
                    transport_staff(),
                    school=trip.school,
                    category=Category.SAFETY,
                    title=f"{_vehicle_label(trip)}: bus-empty check missing",
                    body="The trip was closed automatically without the crew confirming the bus was empty. Check the bus now.",
                    data={"trip_id": str(trip.id), "type": "empty_check_missing"},
                    dedupe_key=f"bus:{trip.id}:emptycheck",
                    priority=Priority.CRITICAL,
                )
                _publish(trip, route_plan(trip.route, trip.direction))
                summary["auto_closed"] += 1
    return summary


# ---------------------------------------------------------------- summaries


def student_bus_summary(student) -> dict:
    """Compact bus status for a child's home card."""
    assignment = (
        StudentTransport.objects.filter(student=student, is_active=True)
        .select_related("route", "route__vehicle", "pickup_stop", "drop_stop")
        .first()
    )
    if assignment is None:
        return {"enrolled": False}
    route = assignment.route
    today = school_today(route.school)
    ensure_trips_for_date(route, today)
    trips = list(
        Trip.objects.filter(route=route, service_date=today).select_related(
            "route", "vehicle", "school", "driver", "attendant"
        )
    )
    absent = set(
        TransportAbsence.objects.filter(student=student, service_date=today).values_list("direction", flat=True)
    )
    summary = {
        "enrolled": True,
        "route_name": route.name,
        "vehicle_label": route.vehicle.label if route.vehicle else None,
        "pickup_stop": assignment.pickup_stop.name,
        "drop_stop": assignment.drop_stop.name,
        "pickup_stop_id": str(assignment.pickup_stop_id),
        "drop_stop_id": str(assignment.drop_stop_id),
        "status": "done",
        "trip_id": None,
    }
    active = next((t for t in trips if t.status == Trip.Status.ACTIVE), None)
    upcoming = next((t for t in sorted(trips, key=lambda t: t.scheduled_start) if t.status == Trip.Status.SCHEDULED), None)
    if active is not None:
        my_stop = assignment.pickup_stop if active.direction == Direction.PICKUP else assignment.drop_stop
        live = live_state(active)
        mine = next((s for s in live["stops"] if s["id"] == str(my_stop.id)), None)
        summary.update(
            status="active",
            trip_id=str(active.id),
            direction=active.direction,
            stop_name=my_stop.name,
            my_stop_status=mine["status"] if mine else None,
            eta_seconds=mine["eta_seconds"] if mine else None,
            signal=live["signal"],
            next_stop=live["next_stop"]["name"] if live["next_stop"] else None,
            absent=bool(absent & {active.direction, TransportAbsence.Scope.BOTH}),
        )
    elif upcoming is not None:
        summary.update(
            status="scheduled",
            trip_id=str(upcoming.id),
            direction=upcoming.direction,
            scheduled_start=upcoming.scheduled_start.strftime("%H:%M"),
            absent=bool(absent & {upcoming.direction, TransportAbsence.Scope.BOTH}),
        )
    return summary
