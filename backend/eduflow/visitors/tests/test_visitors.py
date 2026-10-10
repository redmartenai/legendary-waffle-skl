"""Visitors: pre-registration and security approval, gate registration, hashed single-use QR passes, the
day rule, reissue, cancellation, scopes, isolation and RLS."""

import datetime
import hashlib
import uuid

import pytest
from django.utils import timezone

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.notifications.models import Notification
from eduflow.visitors.models import Visit

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


@pytest.fixture
def guard(world, make_member):
    return make_member(world.school, roles=["security"])


def _register(client, expect=201, **extra):
    body = {"visitor_name": "Ravi Kumar", "phone": "+919800000000", "purpose": "Meeting", **extra}
    return _ok(client.post("/api/v1/visits", body, format="json"), expect)


def test_a_host_pre_registers_and_security_approves(world, guard, as_member):
    teacher, gate = as_member(world.teacher), as_member(guard)
    created = _register(teacher)
    assert (created["visit"]["status"], created["pass_token"], created["visit"]["host"]) == (
        "pending",
        None,
        world.teacher.user.full_name,
    )
    _ok(
        teacher.post(
            f"/api/v1/visits/{created['visit']['id']}/decision", {"decision": "approve"}, format="json"
        ),
        403,
    )
    decided = _ok(
        gate.post(f"/api/v1/visits/{created['visit']['id']}/decision", {"decision": "approve"}, format="json")
    )
    token = decided["pass_token"]
    visit = Visit.objects.get()
    assert visit.pass_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(Visit.objects.values().get())
    assert Notification.objects.filter(recipient=world.teacher, title__endswith="approved").exists()
    inside = _ok(gate.post("/api/v1/visits/scan", {"token": token}, format="json"))
    assert inside["status"] == "inside"
    assert Notification.objects.filter(recipient=world.teacher, title="Ravi Kumar has arrived").exists()
    assert [v["id"] for v in _ok(gate.get("/api/v1/visits/inside"))] == [inside["id"]]
    assert _ok(gate.post("/api/v1/visits/scan", {"token": token}, format="json"))["status"] == "left"
    _ok(gate.post("/api/v1/visits/scan", {"token": token}, format="json"), 404)  # single use
    _ok(teacher.post("/api/v1/visits/scan", {"token": token}, format="json"), 403)


def test_gate_registration_is_approved_at_once(world, guard, as_member):
    created = _register(as_member(guard), host_id=str(world.teacher.pk))
    assert created["visit"]["status"] == "approved"
    assert created["pass_token"]


def test_passes_work_only_on_the_day_and_can_be_reissued(world, guard, as_member):
    tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
    teacher, gate = as_member(world.teacher), as_member(guard)
    created = _register(teacher, expected_on=tomorrow)
    first = _ok(
        gate.post(f"/api/v1/visits/{created['visit']['id']}/decision", {"decision": "approve"}, format="json")
    )["pass_token"]
    _ok(gate.post("/api/v1/visits/scan", {"token": first}, format="json"), 409)  # not today
    second = _ok(teacher.post(f"/api/v1/visits/{created['visit']['id']}/pass"))["pass_token"]
    assert second != first
    _ok(gate.post("/api/v1/visits/scan", {"token": first}, format="json"), 404)
    _ok(teacher.post(f"/api/v1/visits/{created['visit']['id']}/cancel"))
    _ok(gate.post("/api/v1/visits/scan", {"token": second}, format="json"), 404)
    _register(teacher, expected_on="2020-01-01", expect=400)


def test_hosts_see_only_their_visits(world, guard, as_member):
    _register(as_member(world.teacher))
    _register(as_member(world.parent), student_id=str(world.student.pk))
    _register(as_member(world.parent), student_id=str(world.other_student.pk), expect=400)
    assert len(_ok(as_member(world.teacher).get("/api/v1/visits"))["results"]) == 1
    assert len(_ok(as_member(guard).get("/api/v1/visits"))["results"]) == 2
    assert as_member(world.student_member).get("/api/v1/visits").status_code == 403


VISITOR_MATRIX = [
    ("get", "/api/v1/visits/{visit}", None),
    ("post", "/api/v1/visits/{visit}/decision", {"decision": "approve"}),
    ("post", "/api/v1/visits/{visit}/pass", None),
    ("post", "/api/v1/visits/{visit}/cancel", None),
]
VISITOR_MATRIX_PATHS = {p for _, p, _ in VISITOR_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), VISITOR_MATRIX)
def test_another_schools_visit_answers_like_unknown(world, other_world, as_member, method, path, body):
    visit = Visit.objects.create(
        school=other_world.school,
        visitor_name="X",
        phone="1",
        purpose="x",
        expected_on=timezone.localdate(),
        registered_by=other_world.admin,
    )
    client = as_member(world.admin)

    def call(pk):
        url = path.format(visit=pk)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(visit.pk).status_code == call(uuid.uuid4()).status_code == 404
    assert Visit.objects.get(pk=visit.pk).status == "pending"


def test_a_foreign_pass_does_not_scan(world, other_world, guard, as_member, make_member):
    other_guard = make_member(other_world.school, roles=["security"])
    token = _register(as_member(other_guard))["pass_token"]
    _ok(as_member(guard).post("/api/v1/visits/scan", {"token": token}, format="json"), 404)


def test_visits_are_under_rls(world, other_world):
    Visit.objects.create(
        school=other_world.school,
        visitor_name="X",
        phone="1",
        purpose="x",
        expected_on=timezone.localdate(),
        registered_by=other_world.admin,
    )
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Visit.objects.exists()
