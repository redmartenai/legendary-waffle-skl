"""Console: transport · live (fleet, route run, notify a route's families, manage routes, exceptions)."""

import random
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.academics.models import Student
from apps.accounts.models import AuditLog
from apps.announcements.models import Announcement
from apps.core.seed_console.ops.transport import _run
from apps.core.utils import school_today, school_tz
from apps.transport import services
from apps.transport.models import Route, TransportException, Trip, TripIncident, Vehicle

from .conftest import PARENT_MEERA, PRINCIPAL, TEACHER_ANITA, api

URL = "/api/v1/console/transport"


def _drop_trip(route):
    today = school_today(route.school)
    services.ensure_trips_for_date(route, today)
    return Trip.objects.get(route=route, direction="drop", service_date=today)


def _run_late(route, minutes_late=12):
    """Replay a drop run through the engine: the bus stands at its third stop, ``minutes_late`` behind its timetable."""
    plan = services.route_plan(route, "drop")
    hold = plan.stops[2]
    tz = school_tz(route.school)
    start = (timezone.now() - timedelta(minutes=hold.offset_min + minutes_late, seconds=30)).astimezone(tz).replace(second=0, microsecond=0)
    if start.date() != school_today(route.school):
        pytest.skip("Just after midnight the replay would start yesterday; buses don't run then.")
    trip = _drop_trip(route)
    until = start + timedelta(minutes=hold.offset_min + minutes_late, seconds=5)
    _run(trip, start, until, random.Random(1), depart_after_s=0, speed_mps=9.0, absent=[], hold_at=hold.name)
    trip.refresh_from_db()
    return trip, hold


@pytest.fixture
def principal(ghis):
    return api(PRINCIPAL, ghis)


def test_live_page_lists_fleet_and_run(in_ghis, principal):
    route = Route.objects.get(code="R4")
    Vehicle.objects.create(registration_no="KA 01 ZZ 0001", label="Spare bus")
    res = principal.get(URL)
    assert res.status_code == 200, res.content
    data = res.json()
    labels = [r["label"] for r in data["fleet"]]
    assert route.name in labels and "Spare bus" in labels
    spare = next(r for r in data["fleet"] if r["label"] == "Spare bus")
    assert spare["status"] == "depot" and spare["kind"] == "vehicle"
    row = next(r for r in data["fleet"] if r["route_id"] == str(route.id))
    assert row["riders"] >= 1 and row["path"]["line"]
    assert data["counts"]["buses"] == len(data["fleet"])
    assert data["counts"]["depot"] >= 1
    assert data["school"] is not None


def test_late_bus_is_first_with_held_stop_and_live_etas(in_ghis, principal):
    route = Route.objects.get(code="R4")
    trip, hold = _run_late(route, minutes_late=12)
    TripIncident.objects.create(trip=trip, kind=TripIncident.Kind.DELAY, at=timezone.now(), details={"reason": "Road works at the flyover"})
    data = principal.get(URL).json()
    first = data["fleet"][0]
    assert first["route_id"] == str(route.id)
    assert first["status"] == "late" and first["delay"] == 12
    assert first["position"] is not None
    assert first["held"]["stop"] == hold.name
    assert data["counts"]["late"] >= 1 and data["run"]["direction"] == "drop"
    assert data["selected"] == str(route.id)

    detail = principal.get(f"{URL}/routes/{route.id}").json()
    assert detail["status"] == "late" and detail["delay"] == 12
    assert detail["reason"] == "Road works at the flyover"
    assert detail["on_board"] == detail["riders"]  # nobody absent: everyone boarded at the gate
    held = next(s for s in detail["stops"] if s["name"] == hold.name)
    assert held["state"] == "at_stop" and held["held_minutes"] >= 3
    ahead = [s for s in detail["stops"] if s["state"] == "upcoming"]
    assert ahead, "stops still ahead"
    for s in ahead:  # planned + 12 min
        h, m = map(int, s["planned"].split(":"))
        eh, em = map(int, s["eta"].split(":"))
        assert ((eh * 60 + em) - (h * 60 + m)) % (24 * 60) == 12
    assert {c["role"] for c in detail["crew"]} <= {"driver", "attendant"}


def test_route_detail_404_for_unknown_route(in_ghis, principal):
    assert principal.get(f"{URL}/routes/{uuid.uuid4()}").status_code == 404


def test_exceptions_today_are_listed(in_ghis, principal):
    route = Route.objects.get(code="R4")
    kid = Student.objects.filter(transport__route=route).first()
    item = TransportException.objects.create(kind=TransportException.Kind.NOT_SCANNED, route=route, occurred_at=timezone.now(), note="Boarded without ID scan")
    item.students.set([kid])
    TransportException.objects.create(kind=TransportException.Kind.HELD, route=route, occurred_at=timezone.now() - timedelta(days=2), note="Old")
    data = principal.get(URL).json()["exceptions"]
    assert data["open"] == 1
    assert [e["kind"] for e in data["items"]] == ["not_scanned"]
    assert data["items"][0]["students"][0]["name"] == kid.full_name
    assert data["items"][0]["route"] == route.name


def test_notify_route_parents_sends_and_audits(in_ghis, principal):
    route = Route.objects.get(code="R4")
    res = principal.post(f"{URL}/routes/{route.id}/notify", {"title": "Route 4 running late", "body": "The bus is 12 min late."}, format="json")
    assert res.status_code == 201, res.content
    item = Announcement.objects.get(id=res.json()["id"])
    assert item.audience == Announcement.Audience.ROUTE and item.route_id == route.id
    assert item.kind == Announcement.Kind.TRANSPORT and item.delivered_at is not None
    assert res.json()["recipients"] > 0
    assert AuditLog.objects.filter(action="transport.notify_parents", target_id=str(item.id)).exists()
    # A second tap right away is refused (no double messages to families).
    again = principal.post(f"{URL}/routes/{route.id}/notify", {"title": "Again", "body": "Again"}, format="json")
    assert again.status_code == 400
    assert "route" in again.json()["error"]["fields"]


def test_notify_validation(in_ghis, principal):
    route = Route.objects.get(code="R4")
    res = principal.post(f"{URL}/routes/{route.id}/notify", {"title": "", "body": ""}, format="json")
    assert res.status_code == 400
    assert set(res.json()["error"]["fields"]) == {"title", "body"}
    res = principal.post(f"{URL}/routes/{route.id}/notify", {"title": "x", "body": "y" * 1001}, format="json")
    assert res.status_code == 400 and "body" in res.json()["error"]["fields"]
    empty = Route.objects.create(code="R99", name="Route 99", path=[[17.44, 78.38], [17.45, 78.39]])
    res = principal.post(f"{URL}/routes/{empty.id}/notify", {"title": "Hi", "body": "Hello"}, format="json")
    assert res.status_code == 400 and "route" in res.json()["error"]["fields"]
    assert not Announcement.objects.filter(route=empty).exists()


def test_manage_routes_lists_stops_and_spare_buses(in_ghis, principal):
    Vehicle.objects.create(registration_no="KA 01 ZZ 0002", label="Spare 2")
    data = principal.get(f"{URL}/routes").json()
    route = next(r for r in data["routes"] if r["code"] == "R4")
    assert route["stops"] and route["riders"] >= 1
    assert any(s["is_school"] for s in route["stops"])
    assert "Spare 2" in [v["label"] for v in data["spare_vehicles"]]


@pytest.mark.parametrize("phone", [TEACHER_ANITA, PARENT_MEERA])
def test_other_roles_are_refused(in_ghis, ghis, phone):
    client = api(phone, ghis)
    route = Route.objects.get(code="R4")
    assert client.get(URL).status_code == 403
    assert client.get(f"{URL}/routes").status_code == 403
    assert client.get(f"{URL}/routes/{route.id}").status_code == 403
    res = client.post(f"{URL}/routes/{route.id}/notify", {"title": "Hi", "body": "Hello"}, format="json")
    assert res.status_code == 403
    assert not Announcement.objects.filter(route=route, title="Hi").exists()
