"""Console: transport · live. Every bus on today's run on the map, the fleet late-first, the selected route's run
(crew, who's on board, stop timeline with planned vs live times), parents on a route notified, and the day's
pickup & drop exceptions.

Lateness is schedule deviation, the usual measure for buses: how far behind its timetable a bus is at the point it
has reached (from the tracking engine's progress along the route), carried forward to the stops still ahead.
"""

import time as clock
from collections import Counter
from datetime import datetime, timedelta

from django.db.models import Count
from django.urls import path
from django.utils import timezone
from rest_framework import status as http
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.accounts.audit import audit
from apps.announcements import delivery
from apps.announcements.models import Announcement
from apps.core.api import SchoolAPIView, get_scoped_or_404
from apps.core.utils import school_today, school_tz
from apps.transport import engine
from apps.transport.models import (
    BoardingEvent,
    Direction,
    Route,
    Stop,
    StudentTransport,
    TransportAbsence,
    TransportException,
    Trip,
    TripIncident,
    Vehicle,
)
from apps.transport.services import ensure_today_trips, route_plan

from .common import CONSOLE_ROLES

LATE_MINUTES = 5  # behind by this much or more is "late" (the dashboard uses the same line)
HELD_MINUTES = 3  # standing at a stop this long is "held", not a normal stop
NOTIFY_COOLDOWN = timedelta(minutes=2)
EXCEPTION_ORDER = {k: i for i, k in enumerate(TransportException.Kind.values)}


def _local(moment: datetime | float | None, tz) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, (int, float)):
        moment = datetime.fromtimestamp(moment, tz=tz)
    return moment.astimezone(tz)


def _hhmm(moment, tz) -> str | None:
    local = _local(moment, tz)
    return local.strftime("%H:%M") if local else None


def _phone_display(phone: str | None) -> str | None:
    if not phone:
        return None
    if phone.startswith("+91") and len(phone) == 13:
        return f"+91 {phone[3:8]} {phone[8:]}"
    return phone


def run_direction(trips: list[Trip], now: datetime) -> str:
    """The run the page is about: the one on the road, else the latest that ran today, else the next one."""
    for direction in (Direction.DROP, Direction.PICKUP):
        if any(t.direction == direction and t.status == Trip.Status.ACTIVE for t in trips):
            return direction
    for direction in (Direction.DROP, Direction.PICKUP):
        if any(t.direction == direction and t.started_at for t in trips):
            return direction
    drops = [t for t in trips if t.direction == Direction.DROP]
    if drops:
        first = min(datetime.combine(t.service_date, t.scheduled_start, tzinfo=now.tzinfo) for t in drops)
        return Direction.DROP if now >= first - timedelta(hours=1) else Direction.PICKUP
    return Direction.PICKUP


class RunView:
    """One trip on today's run, read through the tracking engine."""

    def __init__(self, trip: Trip, tz):
        self.trip, self.tz = trip, tz
        self.plan = route_plan(trip.route, trip.direction)
        self.state = engine.TripState.from_dict(trip.state)
        self.start = datetime.combine(trip.service_date, trip.scheduled_start, tzinfo=tz)
        self.has_fix = self.state.last_fix_at is not None and trip.status in (Trip.Status.ACTIVE, Trip.Status.COMPLETED)
        self.finished = trip.status == Trip.Status.COMPLETED or self.state.final_arrived_at is not None
        self.deviation_s = self._deviation() if trip.status == Trip.Status.ACTIVE and self.has_fix and not self.finished else None

    def planned(self, info: engine.StopInfo) -> datetime:
        return self.start + timedelta(minutes=info.offset_min)

    def _deviation(self) -> float:
        """Seconds behind the timetable at the point the bus has reached (negative when early)."""
        stops, progress = self.plan.stops, self.state.progress_m
        before = [s for s in stops if s.distance_m <= progress + 1] or [stops[0]]
        prev = before[-1]
        after = next((s for s in stops if s.distance_m > progress + 1), None)
        due = self.planned(prev).timestamp()
        if after is not None and after.distance_m > prev.distance_m:
            frac = (progress - prev.distance_m) / (after.distance_m - prev.distance_m)
            due += max(0.0, frac) * (self.planned(after).timestamp() - due)
        return self.state.last_fix_at - due

    @property
    def delay(self) -> int:
        return max(0, round(self.deviation_s / 60)) if self.deviation_s is not None else 0

    @property
    def status(self) -> str:
        trip = self.trip
        if trip.status == Trip.Status.CANCELLED:
            return "cancelled"
        if self.finished:
            return "arrived"
        if trip.status == Trip.Status.SCHEDULED:
            return "scheduled"
        return "late" if self.delay >= LATE_MINUTES else "on_time"

    def signal(self, now: float) -> str:
        return engine.signal_status(self.state, now) if self.trip.status == Trip.Status.ACTIVE else "none"

    def eta(self, info: engine.StopInfo) -> datetime | None:
        """Live time for a stop still ahead: its timetable time plus how late the bus is running now."""
        if self.deviation_s is None:
            return None
        return self.planned(info) + timedelta(seconds=max(0.0, self.deviation_s))

    def stops(self) -> list[dict]:
        st, rows, next_marked = self.state, [], False
        for info in self.plan.stops:
            arrived, departed = st.arrived.get(info.id), st.departed.get(info.id)
            if info.id in st.skipped:
                kind = "skipped"
            elif arrived and (departed or self.finished):
                kind = "departed" if departed else "arrived"
            elif arrived:
                kind = "at_stop"
            elif self.trip.status == Trip.Status.ACTIVE and not next_marked:
                kind = "next"
            else:
                kind = "upcoming"
            if kind in ("at_stop", "next"):
                next_marked = True
            held = None
            if kind == "at_stop" and not self.plan.stop_models[info.id].is_school and st.last_fix_at:
                minutes = int((st.last_fix_at - arrived) // 60)
                held = minutes if minutes >= HELD_MINUTES else None
            eta = self.eta(info) if kind in ("next", "upcoming", "at_stop") else None
            rows.append(
                {
                    "id": info.id,
                    "name": info.name,
                    "lat": info.lat,
                    "lng": info.lng,
                    "is_school": self.plan.stop_models[info.id].is_school,
                    "state": kind,
                    "planned": self.planned(info).strftime("%H:%M"),
                    "eta": _hhmm(eta, self.tz),
                    "arrived": _hhmm(arrived, self.tz),
                    "departed": _hhmm(departed, self.tz),
                    "held_minutes": held,
                }
            )
        return rows

    def path(self) -> dict:
        """The route line in travel order, and the part already driven."""
        geometry = self.plan.geometry
        points = [[round(lat, 6), round(lng, 6)] for lat, lng in geometry.points]
        done: list = []
        if self.has_fix:
            progress = geometry.length_m if self.finished else self.state.progress_m
            done = [p for p, c in zip(points, geometry.cumulative) if c <= progress]
            lat, lng = geometry.point_at(progress)
            done.append([round(lat, 6), round(lng, 6)])
        return {"line": points, "done": done}

    def position(self) -> dict | None:
        st = self.state
        if not self.has_fix or self.finished or st.last_lat is None:
            return None
        return {
            "lat": st.last_lat,
            "lng": st.last_lng,
            "heading": None if st.heading is None else round(st.heading),
            "speed_kmh": st.last_speed_kmh,
            "at": _hhmm(st.last_fix_at, self.tz),
        }


def _vehicle(v: Vehicle | None) -> dict | None:
    if v is None:
        return None
    return {
        "id": str(v.id),
        "label": v.label,
        "registration_no": v.registration_no,
        "capacity": v.capacity,
        "gps": v.has_gps_tracker,
        "fitness_valid_until": v.fitness_valid_until.isoformat() if v.fitness_valid_until else None,
        "insurance_valid_until": v.insurance_valid_until.isoformat() if v.insurance_valid_until else None,
        "permit_valid_until": v.permit_valid_until.isoformat() if v.permit_valid_until else None,
    }


def _absent_ids(today, direction) -> set:
    return set(
        TransportAbsence.objects.filter(service_date=today, direction__in=[direction, TransportAbsence.Scope.BOTH]).values_list("student_id", flat=True)
    )


def _boarding_counts(trips) -> tuple[Counter, Counter]:
    rows = BoardingEvent.objects.filter(trip__in=trips).values("trip_id", "kind").annotate(n=Count("student", distinct=True))
    boarded, dropped = Counter(), Counter()
    for row in rows:
        if row["kind"] == BoardingEvent.Kind.BOARDED:
            boarded[row["trip_id"]] = row["n"]
        elif row["kind"] == BoardingEvent.Kind.DROPPED:
            dropped[row["trip_id"]] = row["n"]
    return boarded, dropped


def _delay_reason(trip: Trip) -> str | None:
    incident = TripIncident.objects.filter(trip=trip, kind=TripIncident.Kind.DELAY, resolved_at__isnull=True).order_by("-at").first()
    return (incident.details or {}).get("reason") if incident else None


def _exception(item: TransportException, tz) -> dict:
    students = sorted(item.students.all(), key=lambda s: s.full_name)
    return {
        "id": str(item.id),
        "kind": item.kind,
        "status": item.status,
        "students": [{"id": str(s.id), "name": s.full_name, "class": s.class_group.short_label} for s in students],
        "route": item.route.name if item.route else None,
        "from_stop": item.from_stop.name if item.from_stop else None,
        "to_stop": item.to_stop.name if item.to_stop else None,
        "note": item.note,
        "at": _hhmm(item.occurred_at, tz),
    }


def school_point() -> dict | None:
    stop = Stop.objects.filter(is_school=True, route__is_active=True).order_by("route__code").first()
    return {"name": stop.name, "lat": stop.lat, "lng": stop.lng} if stop else None


class TransportView(SchoolAPIView):
    """The live page: the run, the fleet (late first) with each bus on the map, and today's exceptions."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        tz = school_tz(school)
        now = timezone.now()
        now_ts = clock.time()
        today = school_today(school)
        ensure_today_trips(school)
        trips = list(Trip.objects.filter(service_date=today, route__is_active=True).select_related("route", "vehicle", "school"))
        direction = run_direction(trips, now.astimezone(tz))
        run_trips = {t.route_id: t for t in trips if t.direction == direction}
        riders = Counter(StudentTransport.objects.filter(is_active=True, student__is_active=True).values_list("route_id", flat=True))
        boarded, _dropped = _boarding_counts(list(run_trips.values()))

        fleet, views, on_route = [], [], set()
        for route in Route.objects.filter(is_active=True).select_related("vehicle"):
            trip = run_trips.get(route.id)
            if trip is None:
                continue
            view = RunView(trip, tz)
            views.append(view)
            on_route.add(route.vehicle_id)
            stops = view.stops()
            live = trip.status == Trip.Status.ACTIVE
            at = next((s for s in stops if s["state"] == "at_stop"), None) if live else None
            nxt = next((s for s in stops if s["state"] in ("next", "upcoming")), None) if live else None
            fleet.append(
                {
                    "id": str(route.id),
                    "kind": "route",
                    "route_id": str(route.id),
                    "code": route.code,
                    "label": route.name,
                    "vehicle": _vehicle(route.vehicle),
                    "trip_id": str(trip.id),
                    "status": view.status,
                    "delay": view.delay,
                    "on_board": boarded.get(trip.id, 0),
                    "riders": riders.get(route.id, 0),
                    "signal": view.signal(now_ts),
                    "position": view.position(),
                    "next_stop": {"name": nxt["name"], "eta": nxt["eta"], "planned": nxt["planned"]} if nxt else None,
                    "held": {"stop": at["name"], "minutes": at["held_minutes"]} if at and at["held_minutes"] else None,
                    "start": trip.scheduled_start.strftime("%H:%M"),
                    "path": view.path(),
                    "stops": [{"id": s["id"], "name": s["name"], "lat": s["lat"], "lng": s["lng"], "is_school": s["is_school"], "state": s["state"]} for s in stops],
                }
            )
        for vehicle in Vehicle.objects.filter(is_active=True).exclude(id__in=on_route):
            fleet.append(
                {
                    "id": str(vehicle.id),
                    "kind": "vehicle",
                    "route_id": None,
                    "code": None,
                    "label": vehicle.label,
                    "vehicle": _vehicle(vehicle),
                    "trip_id": None,
                    "status": "depot",
                    "delay": 0,
                    "on_board": 0,
                    "riders": 0,
                    "signal": "none",
                    "position": None,
                    "next_stop": None,
                    "held": None,
                    "start": None,
                    "path": {"line": [], "done": []},
                    "stops": [],
                }
            )
        rank = {"late": 0, "on_time": 1, "scheduled": 2, "cancelled": 3, "arrived": 4, "depot": 5}
        fleet.sort(key=lambda r: (rank[r["status"]], -r["delay"], r["label"]))

        counts = Counter(r["status"] for r in fleet)
        active = [v for v in views if v.trip.status == Trip.Status.ACTIVE]
        started = [v for v in views if v.trip.started_at]
        ends = [
            v.eta(v.plan.stops[-1]) for v in active if v.deviation_s is not None
        ] + [_local(v.trip.ended_at, tz) for v in views if v.trip.status == Trip.Status.COMPLETED and v.trip.ended_at]
        fixes = [v.state.last_fix_at for v in active if v.state.last_fix_at]
        signals = {v.signal(now_ts) for v in active}
        day_start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
        exceptions = sorted(
            TransportException.objects.filter(occurred_at__gte=day_start, occurred_at__lt=day_start + timedelta(days=1))
            .select_related("route", "from_stop", "to_stop")
            .prefetch_related("students__class_group"),
            key=lambda e: (e.status != TransportException.Status.OPEN, EXCEPTION_ORDER.get(e.kind, 9), -e.occurred_at.timestamp()),
        )
        default = next((r["id"] for r in fleet if r["status"] in ("late", "on_time")), fleet[0]["id"] if fleet else None)
        return Response(
            {
                "generated_at": now.isoformat(),
                "updated": _hhmm(now, tz),
                "live": bool(signals & {"live", "weak"}),
                "last_fix": _hhmm(max(fixes), tz) if fixes else None,
                "run": {
                    "direction": direction,
                    "started": min(v.trip.scheduled_start for v in started).strftime("%H:%M") if started else None,
                    "ends": max(ends).strftime("%H:%M") if ends else None,
                    "next_start": min((v.trip.scheduled_start.strftime("%H:%M") for v in views if v.trip.status == Trip.Status.SCHEDULED), default=None),
                },
                "counts": {
                    "buses": len(fleet),
                    "on_road": counts["late"] + counts["on_time"],
                    "late": counts["late"],
                    "arrived": counts["arrived"],
                    "depot": counts["depot"],
                    "scheduled": counts["scheduled"],
                    "cancelled": counts["cancelled"],
                },
                "school": school_point(),
                "fleet": fleet,
                "selected": default,
                "exceptions": {
                    "open": sum(1 for e in exceptions if e.status == TransportException.Status.OPEN),
                    "items": [_exception(e, tz) for e in exceptions],
                },
            }
        )


class RouteRunView(SchoolAPIView):
    """The selected route's run: crew, who's on board and who's absent, and the stop timeline."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, route_id):
        school = request.school
        tz = school_tz(school)
        today = school_today(school)
        route = get_scoped_or_404(Route, id=route_id)
        ensure_today_trips(school)
        trips = list(Trip.objects.filter(service_date=today, route__is_active=True).select_related("route", "vehicle", "school", "driver", "attendant"))
        direction = run_direction(trips, timezone.now().astimezone(tz))
        trip = next((t for t in trips if t.route_id == route.id and t.direction == direction), None)
        if trip is None:
            raise ValidationError({"route": "This route has no trip today."})
        view = RunView(trip, tz)
        stops = view.stops()

        assignments = list(StudentTransport.objects.filter(route=route, is_active=True, student__is_active=True).select_related("student__class_group"))
        absent_ids = _absent_ids(today, trip.direction)
        field = "drop_stop_id" if trip.direction == Direction.DROP else "pickup_stop_id"
        per_stop = Counter(str(getattr(a, field)) for a in assignments if a.student_id not in absent_ids)
        for s in stops:
            s["students"] = per_stop.get(s["id"], 0)
        absent = sorted((a.student for a in assignments if a.student_id in absent_ids), key=lambda s: s.full_name)
        boarded, dropped = _boarding_counts([trip])

        driver, attendant = trip.driver or route.driver, trip.attendant or route.attendant
        crew = [
            {"role": role, "id": str(u.id), "name": u.full_name, "phone": u.phone, "phone_display": _phone_display(u.phone)}
            for role, u in (("driver", driver), ("attendant", attendant))
            if u is not None
        ]
        # The next stop still ahead (not the one the bus is standing at).
        nxt = next((s for s in stops if s["state"] in ("next", "upcoming") and not s["is_school"]), None)
        final = stops[-1] if stops else None
        last_sent = (
            Announcement.objects.filter(audience=Announcement.Audience.ROUTE, route=route, published_at__date__gte=today - timedelta(days=1))
            .order_by("-published_at")
            .first()
        )
        families = len({a.student_id for a in assignments})
        return Response(
            {
                "route": {"id": str(route.id), "code": route.code, "name": route.name},
                "vehicle": _vehicle(trip.vehicle or route.vehicle),
                "trip": {
                    "id": str(trip.id),
                    "direction": trip.direction,
                    "status": trip.status,
                    "start": trip.scheduled_start.strftime("%H:%M"),
                    "started": _hhmm(trip.started_at, tz),
                    "ended": _hhmm(trip.ended_at, tz),
                },
                "status": view.status,
                "delay": view.delay,
                "reason": _delay_reason(trip) if view.status == "late" else None,
                "held": next(({"stop": s["name"], "minutes": s["held_minutes"]} for s in stops if s["held_minutes"]), None),
                "position": view.position(),
                "crew": crew,
                "riders": len(assignments),
                "on_board": boarded.get(trip.id, 0),
                "dropped": dropped.get(trip.id, 0),
                "absent": [{"id": str(s.id), "name": s.full_name, "class": s.class_group.short_label} for s in absent],
                "stops": stops,
                "service_stops": sum(1 for s in stops if not s["is_school"] and per_stop.get(s["id"])),
                "next": {"name": nxt["name"], "planned": nxt["planned"], "eta": nxt["eta"]} if nxt else None,
                "final": {"name": final["name"], "planned": final["planned"], "eta": final["eta"]} if final else None,
                "notify": {
                    "families": families,
                    "last_sent": _hhmm(last_sent.published_at, tz) if last_sent and _local(last_sent.published_at, tz).date() == today else None,
                },
            }
        )


class NotifyRouteView(SchoolAPIView):
    """Tell every family on a route (push + in-app, optionally SMS to families without the app)."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, route_id):
        route = get_scoped_or_404(Route, id=route_id)
        title = (request.data.get("title") or "").strip()
        body = (request.data.get("body") or "").strip()
        errors = {}
        if not title:
            errors["title"] = "Add a title."
        elif len(title) > 120:
            errors["title"] = "Keep the title under 120 characters."
        if not body:
            errors["body"] = "Write the message parents will get."
        elif len(body) > 1000:
            errors["body"] = "Keep the message under 1,000 characters."
        if errors:
            raise ValidationError(errors)
        if not route.is_active or not StudentTransport.objects.filter(route=route, is_active=True).exists():
            raise ValidationError({"route": f"No families ride {route.name}."})
        now = timezone.now()
        recent = Announcement.objects.filter(audience=Announcement.Audience.ROUTE, route=route, published_at__gte=now - NOTIFY_COOLDOWN).exists()
        if recent:
            raise ValidationError({"route": f"Families on {route.name} were just notified. Wait a couple of minutes before sending again."})
        channels = ["push", "in_app"] + (["sms"] if request.data.get("sms") in (True, "true", "1", 1) else [])
        item = Announcement.objects.create(
            title=title,
            body=body,
            kind=Announcement.Kind.TRANSPORT,
            audience=Announcement.Audience.ROUTE,
            route=route,
            created_by=request.user,
            published_at=now,
            channels=channels,
        )
        counts = delivery.deliver(item, exclude=request.user)
        audit(
            request,
            "transport.notify_parents",
            target=item,
            summary=f"Notified families on {route.name}: {title}",
            detail={"route": route.code, "recipients": item.recipients, "channels": channels},
        )
        return Response({"id": str(item.id), "recipients": item.recipients, "delivered": counts}, status=http.HTTP_201_CREATED)


class RoutesView(SchoolAPIView):
    """Manage routes: every route with its bus, crew, riders and stops (in pickup order, with both timetables)."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        riders = Counter(StudentTransport.objects.filter(is_active=True, student__is_active=True).values_list("route_id", flat=True))
        per_stop = Counter(StudentTransport.objects.filter(is_active=True, student__is_active=True).values_list("pickup_stop_id", flat=True))
        routes = []
        for route in Route.objects.select_related("vehicle", "driver", "attendant").prefetch_related("stops"):
            routes.append(
                {
                    "id": str(route.id),
                    "code": route.code,
                    "name": route.name,
                    "active": route.is_active,
                    "vehicle": _vehicle(route.vehicle),
                    "driver": route.driver.full_name if route.driver else None,
                    "attendant": route.attendant.full_name if route.attendant else None,
                    "riders": riders.get(route.id, 0),
                    "length_km": round(route.length_m / 1000, 1),
                    "pickup_start": route.pickup_start.strftime("%H:%M"),
                    "drop_start": route.drop_start.strftime("%H:%M"),
                    "stops": [
                        {"id": str(s.id), "name": s.name, "is_school": s.is_school, "pickup": s.pickup_offset_min, "drop": s.drop_offset_min, "riders": per_stop.get(s.id, 0)}
                        for s in sorted(route.stops.all(), key=lambda s: s.sequence)
                    ],
                }
            )
        used = {r.vehicle_id for r in Route.objects.filter(vehicle__isnull=False)}
        spare = [_vehicle(v) for v in Vehicle.objects.filter(is_active=True).exclude(id__in=used)]
        return Response({"routes": routes, "spare_vehicles": spare})


urlpatterns = [
    path("console/transport", TransportView.as_view()),
    path("console/transport/routes", RoutesView.as_view()),
    path("console/transport/routes/<uuid:route_id>", RouteRunView.as_view()),
    path("console/transport/routes/<uuid:route_id>/notify", NotifyRouteView.as_view()),
]
