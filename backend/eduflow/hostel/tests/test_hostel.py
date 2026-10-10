"""Hostel: rooms and bed capacity, allocation, outpass request by a parent, approval in the queue, gate
check-out and late return, roll call, family visibility, isolation and RLS."""

import datetime
import uuid

import pytest
from django.utils import timezone

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.hostel import services
from eduflow.hostel.models import Allocation, Hostel, Outpass, RollCall, Room
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


@pytest.fixture
def boarding(world, as_member):
    admin = as_member(world.admin)
    hostel = _ok(
        admin.post(
            "/api/v1/hostels",
            {"name": "Banyan House", "rooms": [{"number": "101", "beds": 1}]},
            format="json",
        ),
        201,
    )
    room = hostel["rooms"][0]["id"]
    _ok(
        admin.post(
            "/api/v1/hostel/allocations",
            {"student_id": str(world.student.pk), "room_id": room},
            format="json",
        ),
        201,
    )
    return hostel


def _outpass(client, student, expect=201, hours=2):
    start = timezone.now() + datetime.timedelta(hours=1)
    body = {
        "student_id": str(student.pk),
        "leave_at": start.isoformat(),
        "return_by": (start + datetime.timedelta(hours=hours)).isoformat(),
        "reason": "Family visit",
    }
    return _ok(client.post("/api/v1/hostel/outpasses", body, format="json"), expect)


def test_rooms_fill_up(world, boarding, as_member):
    admin = as_member(world.admin)
    room = boarding["rooms"][0]["id"]
    _ok(
        admin.post(
            "/api/v1/hostel/allocations",
            {"student_id": str(world.other_student.pk), "room_id": room},
            format="json",
        ),
        409,
    )
    assert _ok(admin.get(f"/api/v1/hostels/{boarding['id']}"))["rooms"][0]["occupied"] == 1
    _ok(
        admin.post(f"/api/v1/hostels/{boarding['id']}/rooms", {"number": "101", "beds": 2}, format="json"),
        409,
    )
    allocation = Allocation.objects.get()
    _ok(admin.delete(f"/api/v1/hostel/allocations/{allocation.pk}"), 204)
    _ok(
        admin.post(
            "/api/v1/hostel/allocations",
            {"student_id": str(world.other_student.pk), "room_id": room},
            format="json",
        ),
        201,
    )


def test_outpass_lifecycle(world, boarding, as_member):
    parent = as_member(world.parent)
    outpass = _outpass(parent, world.student)
    _outpass(parent, world.other_student, expect=400)  # not their child
    _outpass(as_member(world.admin), world.other_student, expect=400)  # not a boarder
    queue = _ok(as_member(world.principal).get("/api/v1/approvals"))
    assert [i["kind"] for i in queue] == ["outpass"]
    admin = as_member(world.admin)
    _ok(
        admin.post(f"/api/v1/hostel/outpasses/{outpass['id']}/gate", {"action": "check_out"}, format="json"),
        409,
    )
    _ok(
        as_member(world.principal).post(
            f"/api/v1/approvals/outpass/{outpass['id']}/decision", {"decision": "approve"}, format="json"
        )
    )
    assert Notification.objects.filter(recipient=world.parent, title="Outpass approved").exists()
    _ok(admin.post(f"/api/v1/hostel/outpasses/{outpass['id']}/gate", {"action": "check_out"}, format="json"))
    past = timezone.now() - datetime.timedelta(hours=3)
    Outpass.objects.filter(pk=outpass["id"]).update(
        leave_at=past, return_by=past + datetime.timedelta(hours=1)
    )
    assert [o.pk for o in services.overdue_outpasses(world.school)] == [uuid.UUID(outpass["id"])]
    back = _ok(
        admin.post(f"/api/v1/hostel/outpasses/{outpass['id']}/gate", {"action": "check_in"}, format="json")
    )
    assert back["status"] == "returned"
    assert Notification.objects.filter(recipient=world.parent, title="Returned late").exists()
    other = _outpass(as_member(world.student_member), world.student)
    _ok(parent.post(f"/api/v1/hostel/outpasses/{other['id']}/cancel"), 403)  # not the requester
    _ok(as_member(world.student_member).post(f"/api/v1/hostel/outpasses/{other['id']}/cancel"))


def test_roll_call_and_visibility(world, boarding, as_member):
    admin = as_member(world.admin)
    body = {"date": "2026-07-14", "entries": [{"student_id": str(world.student.pk), "status": "present"}]}
    assert _ok(admin.post(f"/api/v1/hostels/{boarding['id']}/roll-call", body, format="json")) == {
        "recorded": 1
    }
    bad = {**body, "entries": [{"student_id": str(world.other_student.pk), "status": "present"}]}
    _ok(admin.post(f"/api/v1/hostels/{boarding['id']}/roll-call", bad, format="json"), 400)
    assert len(_ok(as_member(world.parent).get("/api/v1/hostel/roll-call"))["results"]) == 1
    assert len(_ok(as_member(world.parent).get("/api/v1/hostel/allocations"))["results"]) == 1
    assert as_member(world.teacher).get("/api/v1/hostel/allocations").status_code == 403


HOSTEL_MATRIX = [
    ("get", "/api/v1/hostels/{hostel}", None),
    ("post", "/api/v1/hostels/{hostel}/rooms", {"number": "9", "beds": 1}),
    (
        "post",
        "/api/v1/hostels/{hostel}/roll-call",
        {"date": "2026-07-01", "entries": [{"student_id": "{student}", "status": "present"}]},
    ),
    ("get", "/api/v1/hostel/allocations/{allocation}", None),
    ("delete", "/api/v1/hostel/allocations/{allocation}", None),
    ("get", "/api/v1/hostel/outpasses/{outpass}", None),
    ("post", "/api/v1/hostel/outpasses/{outpass}/gate", {"action": "check_out"}),
    ("post", "/api/v1/hostel/outpasses/{outpass}/cancel", None),
]
HOSTEL_MATRIX_PATHS = {p for _, p, _ in HOSTEL_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), HOSTEL_MATRIX)
def test_another_schools_hostel_answers_like_unknown(world, other_world, as_member, method, path, body):
    s = other_world.school
    hostel = Hostel.objects.create(school=s, name="H")
    room = Room.objects.create(school=s, hostel=hostel, number="1", beds=2)
    allocation = Allocation.objects.create(
        school=s, student=other_world.student, room=room, start_date="2026-07-01"
    )
    now = timezone.now()
    outpass = Outpass.objects.create(
        school=s,
        student=other_world.student,
        leave_at=now,
        return_by=now + datetime.timedelta(hours=1),
        reason="x",
        requested_by=other_world.parent,
        status="approved",
    )
    real = {
        "hostel": hostel.pk,
        "allocation": allocation.pk,
        "outpass": outpass.pk,
        "student": other_world.student.pk,
    }
    missing = {**{k: uuid.uuid4() for k in real}, "student": other_world.student.pk}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        payload = None
        if body:
            payload = {
                k: (
                    [{kk: str(vv).format(**values) for kk, vv in e.items()} for e in v]
                    if isinstance(v, list)
                    else v
                )
                for k, v in body.items()
            }
        return (
            getattr(client, method)(url, payload, format="json") if payload else getattr(client, method)(url)
        )

    assert call(real).status_code == call(missing).status_code == 404
    assert Outpass.objects.get(pk=outpass.pk).status == "approved"
    assert not RollCall.objects.exists()


def test_hostel_is_under_rls(world, other_world):
    Hostel.objects.create(school=other_world.school, name="H")
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Hostel.objects.exists()
