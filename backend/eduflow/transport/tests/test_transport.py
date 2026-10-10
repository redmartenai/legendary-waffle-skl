"""Transport: routes and stops, riders and capacity, driver-run trips, delays that notify families, reported
positions only, maintenance, scopes for families and drivers, isolation and RLS."""

import datetime
import uuid

import pytest
from django.utils import timezone

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.notifications.models import Notification
from eduflow.people.models import StaffProfile
from eduflow.transport import services
from eduflow.transport.models import Maintenance, Position, Rider, Route, Stop, Trip, Vehicle

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


@pytest.fixture
def driver(world, make_member):
    member = make_member(world.school, roles=["driver"])
    StaffProfile.objects.create(
        school=world.school, membership=member, employee_id="D-1", staff_type="non_teaching"
    )
    return member


@pytest.fixture
def route(world, as_member, driver):
    admin = as_member(world.admin)
    vehicle = _ok(
        admin.post(
            "/api/v1/transport/vehicles",
            {"registration_number": "KA01AB1234", "label": "Bus 17", "capacity": 1},
            format="json",
        ),
        201,
    )
    body = {
        "name": "North",
        "code": "north",
        "vehicle_id": vehicle["id"],
        "driver_id": str(driver.staff_profile.pk),
        "stops": [
            {
                "name": "Gate",
                "sequence": 1,
                "pickup_time": "07:10",
                "latitude": "12.971599",
                "longitude": "77.594566",
            },
            {"name": "Park", "sequence": 2, "pickup_time": "07:20"},
        ],
    }
    created = _ok(admin.post("/api/v1/transport/routes", body, format="json"), 201)
    _ok(
        admin.post(
            "/api/v1/transport/riders",
            {
                "student_id": str(world.student.pk),
                "route_id": created["id"],
                "stop_id": created["stops"][0]["id"],
            },
            format="json",
        ),
        201,
    )
    return created


def test_route_setup_and_capacity(world, route, as_member):
    admin = as_member(world.admin)
    assert [s["name"] for s in route["stops"]] == ["Gate", "Park"]
    full = admin.post(
        "/api/v1/transport/riders",
        {
            "student_id": str(world.other_student.pk),
            "route_id": route["id"],
            "stop_id": route["stops"][1]["id"],
        },
        format="json",
    )
    assert full.status_code == 409
    _ok(
        admin.patch(f"/api/v1/transport/routes/{route['id']}", {"stops": []}, format="json"), 409
    )  # riders on it
    bad = admin.post(
        "/api/v1/transport/routes",
        {"name": "X", "code": "x", "stops": [{"name": "A", "sequence": 1, "latitude": "12.9"}]},
        format="json",
    )
    assert bad.status_code == 400
    _ok(
        admin.post(
            "/api/v1/transport/vehicles", {"registration_number": "KA01AB1234", "capacity": 3}, format="json"
        ),
        409,
    )


def test_families_and_drivers_see_their_routes(world, route, as_member, driver):
    as_member(world.admin).post("/api/v1/transport/routes", {"name": "South", "code": "south"}, format="json")
    for member in (world.parent, world.student_member, driver):
        names = [r["name"] for r in _ok(as_member(member).get("/api/v1/transport/routes"))["results"]]
        assert names == ["North"], member
    assert (
        _ok(as_member(world.parent).get("/api/v1/transport/riders"))["results"][0]["student"]["full_name"]
        == "Asha"
    )
    assert as_member(world.teacher).get("/api/v1/transport/routes").status_code == 403


def test_trips_delays_and_positions(world, route, as_member, driver):
    drv = as_member(driver)
    trip = _ok(
        drv.post(f"/api/v1/transport/routes/{route['id']}/trips", {"direction": "morning"}, format="json")
    )
    assert (trip["status"], trip["last_position"]) == ("on_route", None)
    _ok(
        drv.post(f"/api/v1/transport/routes/{route['id']}/trips", {"direction": "morning"}, format="json"),
        409,
    )
    _ok(drv.post(f"/api/v1/transport/trips/{trip['id']}/delay", {"minutes": 5}, format="json"))
    assert not Notification.objects.filter(kind="bus").exists()
    _ok(
        drv.post(
            f"/api/v1/transport/trips/{trip['id']}/delay", {"minutes": 12, "reason": "Traffic"}, format="json"
        )
    )
    assert (
        Notification.objects.get(recipient=world.parent, kind="bus").title == "Bus 17 is running 12 min late"
    )
    assert [t.pk for t in services.delayed_trips(world.school, timezone.localdate())] == [
        uuid.UUID(trip["id"])
    ]
    now = timezone.now()
    _ok(
        drv.post(
            f"/api/v1/transport/trips/{trip['id']}/positions",
            {
                "latitude": "12.97",
                "longitude": "77.59",
                "recorded_at": (now + datetime.timedelta(hours=1)).isoformat(),
            },
            format="json",
        ),
        400,
    )
    _ok(
        drv.post(
            f"/api/v1/transport/trips/{trip['id']}/positions",
            {"latitude": "95", "longitude": "77.59", "recorded_at": now.isoformat()},
            format="json",
        ),
        400,
    )
    _ok(
        drv.post(
            f"/api/v1/transport/trips/{trip['id']}/positions",
            {"latitude": "12.975", "longitude": "77.591", "recorded_at": now.isoformat()},
            format="json",
        ),
        201,
    )
    seen = _ok(as_member(world.parent).get(f"/api/v1/transport/trips/{trip['id']}"))
    assert seen["last_position"]["latitude"] == "12.975000"
    _ok(
        as_member(world.parent).post(
            f"/api/v1/transport/trips/{trip['id']}/positions",
            {"latitude": "1", "longitude": "1", "recorded_at": now.isoformat()},
            format="json",
        ),
        403,
    )
    _ok(drv.post(f"/api/v1/transport/trips/{trip['id']}/arrive"))
    _ok(
        drv.post(
            f"/api/v1/transport/trips/{trip['id']}/positions",
            {"latitude": "12.9", "longitude": "77.5", "recorded_at": now.isoformat()},
            format="json",
        ),
        409,
    )


def test_a_driver_cannot_run_another_route(world, route, as_member, driver, make_member):
    other = make_member(world.school, roles=["driver"])
    StaffProfile.objects.create(
        school=world.school, membership=other, employee_id="D-2", staff_type="non_teaching"
    )
    assert (
        as_member(other)
        .post(f"/api/v1/transport/routes/{route['id']}/trips", {"direction": "morning"}, format="json")
        .status_code
        == 404
    )


def test_maintenance_and_compliance(world, route, as_member):
    admin = as_member(world.admin)
    vehicle = Vehicle.objects.get()
    _ok(
        admin.post(
            "/api/v1/transport/maintenance",
            {"vehicle_id": str(vehicle.pk), "date": "2026-07-01", "kind": "Service", "cost": "4500"},
            format="json",
        ),
        201,
    )
    assert services.compliance_due(world.school) == []
    Vehicle.objects.filter(pk=vehicle.pk).update(
        insurance_expires_on=timezone.localdate() + datetime.timedelta(days=10)
    )
    assert services.compliance_due(world.school) == [vehicle]


TRANSPORT_MATRIX = [
    ("get", "/api/v1/transport/vehicles/{vehicle}", None),
    ("patch", "/api/v1/transport/vehicles/{vehicle}", {"label": "x"}),
    ("get", "/api/v1/transport/routes/{route}", None),
    ("patch", "/api/v1/transport/routes/{route}", {"name": "x"}),
    ("post", "/api/v1/transport/routes/{route}/trips", {"direction": "morning"}),
    ("get", "/api/v1/transport/riders/{rider}", None),
    ("delete", "/api/v1/transport/riders/{rider}", None),
    ("get", "/api/v1/transport/trips/{trip}", None),
    ("post", "/api/v1/transport/trips/{trip}/arrive", None),
    ("post", "/api/v1/transport/trips/{trip}/delay", {"minutes": 1}),
    (
        "post",
        "/api/v1/transport/trips/{trip}/positions",
        {"latitude": "1", "longitude": "1", "recorded_at": "2026-01-01T00:00:00Z"},
    ),
    ("get", "/api/v1/transport/maintenance/{maintenance}", None),
]
TRANSPORT_MATRIX_PATHS = {p for _, p, _ in TRANSPORT_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), TRANSPORT_MATRIX)
def test_another_schools_transport_answers_like_unknown(world, other_world, as_member, method, path, body):
    s = other_world.school
    vehicle = Vehicle.objects.create(school=s, registration_number="X1", capacity=10)
    r = Route.objects.create(school=s, name="R", code="r", vehicle=vehicle)
    stop = Stop.objects.create(school=s, route=r, name="S", sequence=1)
    rider = Rider.objects.create(school=s, student=other_world.student, route=r, stop=stop)
    trip = Trip.objects.create(
        school=s,
        route=r,
        date="2026-07-14",
        direction="morning",
        status="on_route",
        started_at=timezone.now(),
    )
    m = Maintenance.objects.create(school=s, vehicle=vehicle, date="2026-07-01", kind="x")
    real = {"vehicle": vehicle.pk, "route": r.pk, "rider": rider.pk, "trip": trip.pk, "maintenance": m.pk}
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(real).status_code == call(missing).status_code == 404
    assert Trip.objects.get(pk=trip.pk).status == "on_route"
    assert not Position.objects.exists()


def test_transport_is_under_rls(world, other_world):
    Vehicle.objects.create(school=other_world.school, registration_number="X1", capacity=10)
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Vehicle.objects.exists()
