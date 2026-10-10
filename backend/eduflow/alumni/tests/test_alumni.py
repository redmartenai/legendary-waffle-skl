"""Alumni: directory with consent, events with capacity, campaigns with sequential donation receipts,
isolation and RLS."""

import uuid

import pytest

from eduflow.alumni.models import Alumnus, Campaign, Event
from eduflow.core import db_context
from eduflow.core.db_context import DbContext

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


def test_directory_events_and_campaigns(world, as_member):
    admin = as_member(world.admin)
    linked = _ok(
        admin.post(
            "/api/v1/alumni",
            {"student_id": str(world.other_student.pk), "graduation_year": 2020, "consent_to_contact": True},
            format="json",
        ),
        201,
    )
    assert linked["full_name"] == "Ravi"
    _ok(
        admin.post(
            "/api/v1/alumni",
            {"student_id": str(world.other_student.pk), "graduation_year": 2020},
            format="json",
        ),
        409,
    )
    _ok(admin.post("/api/v1/alumni", {"graduation_year": 2019}, format="json"), 400)
    other = _ok(
        admin.post("/api/v1/alumni", {"full_name": "Meera", "graduation_year": 2019}, format="json"), 201
    )
    assert [a["full_name"] for a in _ok(admin.get("/api/v1/alumni?consent_to_contact=true"))["results"]] == [
        "Ravi"
    ]
    event = _ok(
        admin.post(
            "/api/v1/alumni-events",
            {"title": "Reunion", "starts_at": "2026-12-20T17:00:00Z", "capacity": 2},
            format="json",
        ),
        201,
    )
    _ok(
        admin.post(
            f"/api/v1/alumni-events/{event['id']}/registrations",
            {"alumnus_id": linked["id"], "guests": 1},
            format="json",
        ),
        201,
    )
    _ok(
        admin.post(
            f"/api/v1/alumni-events/{event['id']}/registrations", {"alumnus_id": other["id"]}, format="json"
        ),
        409,
    )
    assert _ok(admin.get(f"/api/v1/alumni-events/{event['id']}"))["registered"] == 1
    campaign = _ok(
        admin.post(
            "/api/v1/alumni-campaigns",
            {
                "title": "New library",
                "goal_amount": "500000",
                "starts_on": "2026-01-01",
                "ends_on": "2026-12-31",
            },
            format="json",
        ),
        201,
    )
    first = _ok(
        admin.post(
            f"/api/v1/alumni-campaigns/{campaign['id']}/donations",
            {
                "alumnus_id": linked["id"],
                "amount": "10000",
                "received_on": "2026-02-01",
                "mode": "bank_transfer",
            },
            format="json",
        ),
        201,
    )
    second = _ok(
        admin.post(
            f"/api/v1/alumni-campaigns/{campaign['id']}/donations",
            {"donor_name": "Well-wisher", "amount": "2500", "received_on": "2026-02-02", "mode": "cash"},
            format="json",
        ),
        201,
    )
    assert (first["donor_name"], first["receipt_number"], second["receipt_number"]) == (
        "Ravi",
        "D-000001",
        "D-000002",
    )
    _ok(
        admin.post(
            f"/api/v1/alumni-campaigns/{campaign['id']}/donations",
            {"amount": "1", "received_on": "2026-02-02", "mode": "cash"},
            format="json",
        ),
        400,
    )
    assert _ok(admin.get(f"/api/v1/alumni-campaigns/{campaign['id']}"))["raised"] == "12500.00"
    assert as_member(world.teacher).get("/api/v1/alumni").status_code == 403


ALUMNI_MATRIX = [
    ("get", "/api/v1/alumni/{alumnus}", None),
    ("patch", "/api/v1/alumni/{alumnus}", {"city": "x"}),
    ("get", "/api/v1/alumni-events/{event}", None),
    ("get", "/api/v1/alumni-events/{event}/registrations", None),
    ("post", "/api/v1/alumni-events/{event}/registrations", {"alumnus_id": "{alumnus}"}),
    ("get", "/api/v1/alumni-campaigns/{campaign}", None),
    ("get", "/api/v1/alumni-campaigns/{campaign}/donations", None),
    (
        "post",
        "/api/v1/alumni-campaigns/{campaign}/donations",
        {"donor_name": "x", "amount": "1", "received_on": "2026-01-01", "mode": "cash"},
    ),
]
ALUMNI_MATRIX_PATHS = {p for _, p, _ in ALUMNI_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), ALUMNI_MATRIX)
def test_another_schools_alumni_answer_like_unknown(world, other_world, as_member, method, path, body):
    s = other_world.school
    real = {
        "alumnus": Alumnus.objects.create(school=s, full_name="A", graduation_year=2000).pk,
        "event": Event.objects.create(school=s, title="E", starts_at="2026-12-01T00:00:00Z").pk,
        "campaign": Campaign.objects.create(
            school=s, title="C", goal_amount=1, starts_on="2026-01-01", ends_on="2026-02-01"
        ).pk,
    }
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        payload = {k: v.format(**values) for k, v in body.items()} if body else None
        return (
            getattr(client, method)(url, payload, format="json") if payload else getattr(client, method)(url)
        )

    assert call(real).status_code == call(missing).status_code == 404


def test_alumni_are_under_rls(world, other_world):
    Alumnus.objects.create(school=other_world.school, full_name="A", graduation_year=2000)
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Alumnus.objects.exists()
