import hmac
from datetime import date

from django.conf import settings
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.academics.access import student_for_request
from apps.academics.models import Student, StudentGuardian
from apps.accounts.models import CREW_ROLES, TRANSPORT_STAFF_ROLES, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today
from apps.realtime import client as realtime

from . import services
from .models import (
    BoardingEvent,
    Direction,
    Route,
    StudentTransport,
    TransportAbsence,
    TransportPreference,
    Trip,
    TripIncident,
    TripPosition,
    Vehicle,
)

ALERT_CHOICES = (5, 10, 15, 20)


def _trip_or_404(trip_id) -> Trip:
    trip = (
        Trip.objects.select_related("route", "vehicle", "school", "driver", "attendant")
        .filter(id=trip_id)
        .first()
    )
    if trip is None:
        raise Http404
    return trip


def _stop_payload(stop, when: str | None = None) -> dict:
    return {"id": str(stop.id), "name": stop.name, "lat": stop.lat, "lng": stop.lng, "time": when}


# ---------------------------------------------------------------- families


class StudentTransportView(SchoolAPIView):
    """A child's bus: route, stops, crew first names, today's trips and alert settings."""

    def get(self, request, student_id):
        student = student_for_request(request, student_id, purpose="transport")
        assignment = (
            StudentTransport.objects.filter(student=student, is_active=True)
            .select_related("route", "route__vehicle", "route__driver", "route__attendant", "pickup_stop", "drop_stop")
            .first()
        )
        if assignment is None:
            return Response({"enrolled": False})

        route = assignment.route
        today = school_today(request.school)
        services.ensure_trips_for_date(route, today)
        absences = TransportAbsence.objects.filter(student=student, service_date__gte=today).order_by("service_date")
        absent_today = {a.direction for a in absences if a.service_date == today}

        trips_payload = []
        for trip in Trip.objects.filter(route=route, service_date=today).select_related(
            "route", "vehicle", "school", "driver", "attendant"
        ):
            my_stop = assignment.pickup_stop if trip.direction == Direction.PICKUP else assignment.drop_stop
            entry = {
                "trip_id": str(trip.id),
                "direction": trip.direction,
                "status": trip.status,
                "scheduled_start": trip.scheduled_start.strftime("%H:%M"),
                "absent": bool(absent_today & {trip.direction, TransportAbsence.Scope.BOTH}),
                "my_stop_eta_seconds": None,
                "signal": "none",
                "next_stop": None,
            }
            if trip.status == Trip.Status.ACTIVE:
                live = services.live_state(trip)
                mine = next((s for s in live["stops"] if s["id"] == str(my_stop.id)), None)
                entry.update(
                    my_stop_eta_seconds=mine["eta_seconds"] if mine else None,
                    my_stop_status=mine["status"] if mine else None,
                    signal=live["signal"],
                    next_stop=live["next_stop"]["name"] if live["next_stop"] else None,
                )
            trips_payload.append(entry)

        preference = TransportPreference.objects.filter(user=request.user, student=student).first()
        default_minutes = request.school.policy("transport", "default_alert_minutes") or 10
        return Response(
            {
                "enrolled": True,
                "route": {"id": str(route.id), "code": route.code, "name": route.name},
                "vehicle": {"label": route.vehicle.label, "registration_no": route.vehicle.registration_no}
                if route.vehicle
                else None,
                "crew": {
                    "driver": route.driver.first_name if route.driver else None,
                    "attendant": route.attendant.first_name if route.attendant else None,
                },
                "pickup_stop": _stop_payload(
                    assignment.pickup_stop,
                    _offset_time(route.pickup_start, assignment.pickup_stop.pickup_offset_min),
                ),
                "drop_stop": _stop_payload(
                    assignment.drop_stop,
                    _offset_time(route.drop_start, assignment.drop_stop.drop_offset_min),
                ),
                "today": trips_payload,
                "alert_minutes": preference.alert_minutes if preference else default_minutes,
                "alert_choices": list(ALERT_CHOICES),
                "absences": [{"date": a.service_date.isoformat(), "direction": a.direction} for a in absences],
            }
        )


def _offset_time(start, minutes: int) -> str:
    total = start.hour * 60 + start.minute + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


class TransportPreferenceView(SchoolAPIView):
    allowed_roles = frozenset({Role.PARENT, Role.STUDENT})

    def put(self, request, student_id):
        student = student_for_request(request, student_id, purpose="transport")
        try:
            minutes = int(request.data.get("alert_minutes"))
        except (TypeError, ValueError):
            minutes = None
        if minutes not in ALERT_CHOICES:
            raise ValidationError({"alert_minutes": f"Choose one of {', '.join(map(str, ALERT_CHOICES))}."})
        TransportPreference.objects.update_or_create(
            user=request.user, student=student, defaults={"alert_minutes": minutes}
        )
        return Response({"alert_minutes": minutes})


class TransportAbsenceView(SchoolAPIView):
    """'Not travelling today': the crew skips the wait and the family gets no bus alerts."""

    def _parse(self, request):
        raw_date = request.data.get("date") or request.query_params.get("date")
        direction = request.data.get("direction") or request.query_params.get("direction") or "both"
        if direction not in TransportAbsence.Scope.values:
            raise ValidationError({"direction": "Use pickup, drop or both."})
        try:
            service_date = date.fromisoformat(raw_date) if raw_date else school_today(request.school)
        except ValueError as exc:
            raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc
        if service_date < school_today(request.school):
            raise ValidationError({"date": "That date has passed."})
        return service_date, direction

    def post(self, request, student_id):
        student = student_for_request(request, student_id, purpose="transport")
        if not (request.roles & (TRANSPORT_STAFF_ROLES | {Role.PARENT})):
            raise PermissionDenied()
        service_date, direction = self._parse(request)
        TransportAbsence.objects.get_or_create(
            student=student, service_date=service_date, direction=direction, defaults={"created_by": request.user}
        )
        return Response({"date": service_date.isoformat(), "direction": direction}, status=status.HTTP_201_CREATED)

    def delete(self, request, student_id):
        student = student_for_request(request, student_id, purpose="transport")
        service_date, direction = self._parse(request)
        TransportAbsence.objects.filter(student=student, service_date=service_date, direction=direction).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RouteDetailView(SchoolAPIView):
    """The route line and stops in travel order (static; clients cache it)."""

    def get(self, request, route_id):
        route = Route.objects.filter(id=route_id).first()
        if route is None:
            raise Http404
        direction = request.query_params.get("direction", Direction.PICKUP)
        if direction not in Direction.values:
            raise ValidationError({"direction": "Use pickup or drop."})
        if not self._may_view(request, route):
            raise Http404
        plan = services.route_plan(route, direction)
        return Response(
            {
                "id": str(route.id),
                "code": route.code,
                "name": route.name,
                "direction": direction,
                "length_m": round(plan.geometry.length_m),
                "path": [[round(lat, 6), round(lng, 6)] for lat, lng in plan.geometry.points],
                "stops": [
                    {"id": s.id, "name": s.name, "lat": s.lat, "lng": s.lng, "order": s.order}
                    for s in plan.stops
                ],
                "school_stop_id": next(
                    (sid for sid, stop in plan.stop_models.items() if stop.is_school), None
                ),
            }
        )

    @staticmethod
    def _may_view(request, route) -> bool:
        if request.roles & (TRANSPORT_STAFF_ROLES | CREW_ROLES):
            return True
        student_ids = set(StudentGuardian.objects.filter(user=request.user).values_list("student_id", flat=True))
        student_ids |= set(Student.objects.filter(user=request.user).values_list("id", flat=True))
        return StudentTransport.objects.filter(route=route, student_id__in=student_ids, is_active=True).exists()


class TripLiveView(SchoolAPIView):
    """Polling endpoint and fallback for the realtime channel."""

    def get(self, request, trip_id):
        trip = _trip_or_404(trip_id)
        access = services.trip_access(request, trip)
        if access is None:
            raise Http404
        if access == "family" and trip.status == Trip.Status.ACTIVE:
            services.log_view(trip, request.user)
        return Response(services.live_state(trip, staff=access == "staff"))


class TripSubscriptionView(SchoolAPIView):
    """A short-lived Centrifugo token for trip:{id}, issued only to people allowed to watch."""

    def get(self, request, trip_id):
        trip = _trip_or_404(trip_id)
        access = services.trip_access(request, trip)
        if access is None:
            raise Http404
        if not realtime.enabled():
            return Response({"enabled": False, "poll_interval_seconds": 5})
        if access == "family" and trip.status not in {Trip.Status.SCHEDULED, Trip.Status.ACTIVE}:
            raise PermissionDenied("Live tracking is available only during the trip.")
        channel = realtime.trip_channel(trip.id)
        return Response(
            {
                "enabled": True,
                "channel": channel,
                "token": realtime.subscription_token(request.user, channel, ttl_seconds=1800),
                "expires_in": 1800,
            }
        )


# ---------------------------------------------------------------- crew (driver app)


class PositionSerializer(serializers.Serializer):
    client_id = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True)
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)
    speed = serializers.FloatField(required=False, allow_null=True, min_value=0, help_text="metres/second")
    heading = serializers.FloatField(required=False, allow_null=True)
    accuracy = serializers.FloatField(required=False, allow_null=True, min_value=0)
    recorded_at = serializers.DateTimeField()


class PositionBatchSerializer(serializers.Serializer):
    positions = PositionSerializer(many=True, allow_empty=False, max_length=500)


class CrewTripMixin:
    allowed_roles = CREW_ROLES | TRANSPORT_STAFF_ROLES

    def crew_trip(self, request, trip_id) -> Trip:
        trip = _trip_or_404(trip_id)
        if request.roles & TRANSPORT_STAFF_ROLES:
            return trip
        if request.user.id not in {trip.driver_id, trip.attendant_id}:
            raise Http404
        return trip


def _trip_summary(trip: Trip) -> dict:
    riders = services.riders(trip)
    live = services.live_state(trip, staff=True)
    return {
        "id": str(trip.id),
        "direction": trip.direction,
        "status": trip.status,
        "scheduled_start": trip.scheduled_start.strftime("%H:%M"),
        "route": {"id": str(trip.route_id), "code": trip.route.code, "name": trip.route.name},
        "vehicle": live["vehicle"],
        "riders": sum(1 for r in riders if not r.absent),
        "absent": sum(1 for r in riders if r.absent),
        "next_stop": live["next_stop"],
        "signal": live["signal"],
        "stops_total": len(live["stops"]),
        "stops_done": sum(1 for s in live["stops"] if s["status"] in {"departed", "skipped"}),
    }


class DriverTripsView(CrewTripMixin, SchoolAPIView):
    def get(self, request):
        services.ensure_today_trips(request.school)
        trips = Trip.objects.filter(service_date=school_today(request.school)).select_related(
            "route", "vehicle", "school", "driver", "attendant"
        )
        if not (request.roles & TRANSPORT_STAFF_ROLES):
            mine = [t for t in trips if request.user.id in {t.driver_id, t.attendant_id}]
        else:
            mine = list(trips)
        return Response({"date": school_today(request.school).isoformat(), "trips": [_trip_summary(t) for t in mine]})


class DriverTripDetailView(CrewTripMixin, SchoolAPIView):
    def get(self, request, trip_id):
        trip = self.crew_trip(request, trip_id)
        return Response({"trip": _trip_summary(trip), "live": services.live_state(trip, staff=True)})


class DriverTripStartView(CrewTripMixin, SchoolAPIView):
    def post(self, request, trip_id):
        trip = services.start_trip(self.crew_trip(request, trip_id), request.user)
        return Response(services.live_state(trip, staff=True))


class DriverTripEndView(CrewTripMixin, SchoolAPIView):
    def post(self, request, trip_id):
        confirmed = str(request.data.get("empty_check_confirmed", "")).lower() in {"true", "1", "yes"}
        trip = services.end_trip(self.crew_trip(request, trip_id), request.user, empty_check_confirmed=confirmed)
        return Response(services.live_state(trip, staff=True))


class DriverPositionsView(CrewTripMixin, SchoolAPIView):
    def post(self, request, trip_id):
        trip = self.crew_trip(request, trip_id)
        data = PositionBatchSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fixes = [
            {
                "lat": p["lat"],
                "lng": p["lng"],
                "at": p["recorded_at"].timestamp(),
                "speed_mps": p.get("speed"),
                "heading": p.get("heading"),
                "accuracy_m": p.get("accuracy"),
                "client_id": p.get("client_id") or None,
            }
            for p in data.validated_data["positions"]
        ]
        result = services.ingest_positions(trip, fixes, TripPosition.Source.DRIVER_APP)
        trip.refresh_from_db()
        live = services.live_state(trip, staff=True)
        return Response({**result, "next_stop": live["next_stop"], "signal": live["signal"]})


class DriverRosterView(CrewTripMixin, SchoolAPIView):
    def get(self, request, trip_id):
        trip = self.crew_trip(request, trip_id)
        plan = services.route_plan(trip.route, trip.direction)
        events = {}
        for event in BoardingEvent.objects.filter(trip=trip):
            events.setdefault(event.student_id, {})[event.kind] = event.at.isoformat()
        by_stop: dict = {}
        for rider in services.riders(trip):
            student = rider.student
            by_stop.setdefault(rider.stop_id, []).append(
                {
                    "id": str(student.id),
                    "name": student.full_name,
                    "initials": student.initials,
                    "class_label": student.class_group.short_label,
                    "absent": rider.absent,
                    "boarded_at": events.get(student.id, {}).get(BoardingEvent.Kind.BOARDED),
                    "dropped_at": events.get(student.id, {}).get(BoardingEvent.Kind.DROPPED),
                    "no_show_at": events.get(student.id, {}).get(BoardingEvent.Kind.NO_SHOW),
                }
            )
        stops = [
            {"id": s.id, "name": s.name, "order": s.order, "students": sorted(by_stop.get(s.id, []), key=lambda x: x["name"])}
            for s in plan.stops
        ]
        return Response({"trip_id": str(trip.id), "direction": trip.direction, "stops": stops})


class DriverBoardingView(CrewTripMixin, SchoolAPIView):
    def post(self, request, trip_id):
        trip = self.crew_trip(request, trip_id)
        kind = request.data.get("kind")
        if kind not in BoardingEvent.Kind.values:
            raise ValidationError({"kind": "Use boarded, dropped or no_show."})
        student = Student.objects.filter(id=request.data.get("student_id")).first()
        if student is None:
            raise ValidationError({"student_id": "Unknown student."})
        event = services.record_boarding(trip, student, kind, request.user, request.data.get("client_id"))
        return Response({"student_id": str(student.id), "kind": event.kind, "at": event.at.isoformat()})


class DriverSosView(CrewTripMixin, SchoolAPIView):
    def post(self, request, trip_id):
        trip = self.crew_trip(request, trip_id)

        def _float(name):
            try:
                return float(request.data.get(name))
            except (TypeError, ValueError):
                return None

        incident = services.trigger_sos(
            trip, request.user, _float("lat"), _float("lng"), str(request.data.get("note", ""))[:300]
        )
        return Response({"incident_id": str(incident.id), "at": incident.at.isoformat()}, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- transport desk


class TransportDashboardView(SchoolAPIView):
    allowed_roles = TRANSPORT_STAFF_ROLES

    def get(self, request):
        services.ensure_today_trips(request.school)
        today = school_today(request.school)
        trips = Trip.objects.filter(service_date=today).select_related("route", "vehicle", "school", "driver", "attendant")
        vehicles = Vehicle.objects.filter(is_active=True)
        incidents = TripIncident.objects.filter(resolved_at__isnull=True, at__date__gte=today).select_related("trip")[:30]

        def expiring(value):
            return value is not None and (value - today).days <= 30

        return Response(
            {
                "date": today.isoformat(),
                "trips": [_trip_summary(t) for t in trips],
                "vehicles": [
                    {
                        "id": str(v.id),
                        "label": v.label,
                        "registration_no": v.registration_no,
                        "last_seen_at": v.last_seen_at.isoformat() if v.last_seen_at else None,
                        "compliance": {
                            "gps_tracker": v.has_gps_tracker,
                            "panic_button": v.has_panic_button,
                            "cctv": v.has_cctv,
                            "speed_governor": v.has_speed_governor,
                            "documents_expiring": [
                                name
                                for name, value in (
                                    ("fitness", v.fitness_valid_until),
                                    ("permit", v.permit_valid_until),
                                    ("insurance", v.insurance_valid_until),
                                    ("puc", v.puc_valid_until),
                                )
                                if expiring(value)
                            ],
                        },
                    }
                    for v in vehicles
                ],
                "open_incidents": [
                    {
                        "id": str(i.id),
                        "kind": i.kind,
                        "at": i.at.isoformat(),
                        "trip_id": str(i.trip_id),
                        "details": i.details,
                    }
                    for i in incidents
                ],
            }
        )


# ---------------------------------------------------------------- GPS devices


class TraccarIngestView(APIView):
    """Receives Traccar's JSON forwarding. Authenticated with a shared secret header."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ingest"

    def post(self, request):
        supplied = request.META.get("HTTP_X_INGEST_SECRET") or request.query_params.get("secret") or ""
        expected = settings.EDUFLOW["TRACCAR_SHARED_SECRET"]
        if not expected or not hmac.compare_digest(str(supplied), expected):
            return Response({"error": {"code": "forbidden", "message": "Bad secret."}}, status=403)
        return Response(services.ingest_traccar(request.data))
