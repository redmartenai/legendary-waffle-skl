"""Terms and rooms (academics, Phase 5): validation, lifecycle and database constraints."""

import datetime

import pytest
from django.db import IntegrityError, transaction

from eduflow.academics.models import AcademicYear, Room, Term

pytestmark = pytest.mark.django_db


def _term(api, world, expect=201, **extra):
    body = {
        "academic_year_id": str(world.year.pk),
        "name": "Term 1",
        "code": "t1",
        "start_date": "2026-06-01",
        "end_date": "2026-09-30",
        **extra,
    }
    return api.post("terms", body, expect=expect)


# ------------------------------------------------------------------------------------------------ terms
def test_terms_lie_within_their_year_and_never_overlap(world, admin_api):
    first = _term(admin_api, world)
    assert first["academic_year"]["id"] == str(world.year.pk)
    _term(admin_api, world, name="T2", code="t2", start_date="2026-10-01", end_date="2027-03-31")
    overlap = _term(
        admin_api, world, name="T3", code="t3", start_date="2026-09-15", end_date="2026-10-15", expect=400
    )
    assert "start_date" in overlap["error"]["fields"]
    outside = _term(
        admin_api, world, name="T4", code="t4", start_date="2027-03-01", end_date="2027-04-30", expect=400
    )
    assert "start_date" in outside["error"]["fields"]
    backwards = _term(
        admin_api, world, name="T5", code="t5", start_date="2026-09-01", end_date="2026-08-01", expect=400
    )
    assert "end_date" in backwards["error"]["fields"]


def test_term_overlap_is_also_refused_by_the_database(world):
    Term.objects.create(
        school=world.school,
        academic_year=world.year,
        name="A",
        code="a",
        start_date=datetime.date(2026, 6, 1),
        end_date=datetime.date(2026, 9, 30),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Term.objects.create(
            school=world.school,
            academic_year=world.year,
            name="B",
            code="b",
            start_date=datetime.date(2026, 9, 30),
            end_date=datetime.date(2026, 12, 31),
        )


def test_term_update_rechecks_dates_and_duplicates(world, admin_api):
    first = _term(admin_api, world)
    _term(admin_api, world, name="T2", code="t2", start_date="2026-10-01", end_date="2027-03-31")
    admin_api.patch(f"terms/{first['id']}", {"end_date": "2026-10-10"}, expect=400)
    assert admin_api.patch(f"terms/{first['id']}", {"code": "t2"}, expect=409)["error"]["code"] == "conflict"
    assert admin_api.patch(f"terms/{first['id']}", {"name": "First term"})["name"] == "First term"


def test_terms_of_a_closed_year_are_read_only(world, admin_api):
    term = _term(admin_api, world)
    AcademicYear.objects.filter(pk=world.year.pk).update(status="closed", is_current=False)
    admin_api.patch(f"terms/{term['id']}", {"name": "X"}, expect=409)
    admin_api.delete(f"terms/{term['id']}", expect=409)
    assert (
        "academic_year_id" in _term(admin_api, world, name="Late", code="late", expect=400)["error"]["fields"]
    )


def test_term_in_use_cannot_be_deleted(world, admin_api, new_plan):
    term = _term(admin_api, world)
    new_plan(term_id=term["id"])
    admin_api.delete(f"terms/{term['id']}", expect=409)
    unused = _term(admin_api, world, name="T2", code="t2", start_date="2026-10-01", end_date="2027-03-31")
    admin_api.delete(f"terms/{unused['id']}")


def test_terms_are_school_structure_every_member_reads(world, admin_api, api_as):
    _term(admin_api, world)
    for member in (world.teacher, world.parent, world.student_member):
        assert len(api_as(member).get("terms")["results"]) == 1
        api_as(member).post("terms", {}, expect=403)


# ------------------------------------------------------------------------------------------------ rooms
def test_rooms_are_unique_per_campus_and_code(world, admin_api):
    body = {"name": "Room 101", "code": "r101", "campus_id": str(world.campus.pk), "capacity": 40}
    room = admin_api.post("rooms", body)
    assert room["campus"]["id"] == str(world.campus.pk)
    assert room["kind"] == "classroom"
    admin_api.post("rooms", {**body, "code": "r101b"}, expect=409)  # same name, same campus
    admin_api.post("rooms", {"name": "Room 101", "code": "r101c"})  # no campus: allowed once
    admin_api.post("rooms", {"name": "Room 101", "code": "r101d"}, expect=409)  # ... but only once
    admin_api.post("rooms", {"name": "Lab", "code": "r101"}, expect=409)  # code taken


def test_room_validation(world, admin_api):
    world.campus.status = "archived"
    world.campus.save()
    archived = admin_api.post(
        "rooms", {"name": "X", "code": "x", "campus_id": str(world.campus.pk)}, expect=400
    )
    assert "campus_id" in archived["error"]["fields"]
    admin_api.post("rooms", {"name": "Y", "code": "y", "capacity": 0}, expect=400)
    with pytest.raises(IntegrityError), transaction.atomic():
        Room.objects.create(school=world.school, name="Z", code="z", capacity=0)


def test_room_update_and_delete(world, admin_api, plan):
    room = admin_api.post("rooms", {"name": "Lab", "code": "lab", "kind": "laboratory"})
    assert admin_api.patch(f"rooms/{room['id']}", {"status": "archived"})["status"] == "archived"
    assert admin_api.get("rooms?status=archived")["results"][0]["id"] == room["id"]
    admin_api.delete(f"rooms/{room['id']}")
    used = Room.objects.create(school=world.school, name="Used", code="used")
    plan.slot(1, room=used)
    admin_api.delete(f"rooms/{used.pk}", expect=409)
