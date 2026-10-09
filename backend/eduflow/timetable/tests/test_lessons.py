"""Lessons: the first write a teacher makes with a narrower scope than the whole school (``lesson.manage``
with ``self``), and who may read them."""

import datetime

import pytest

from eduflow.academics.models import AcademicYear
from eduflow.audit.models import AuditEvent
from eduflow.people.models import TeacherAssignment
from eduflow.timetable.models import Lesson

from .conftest import MONDAY, TUESDAY, week

pytestmark = pytest.mark.django_db


@pytest.fixture
def live(world, plan, second_teacher):
    """Published: section A has Maths (world teacher) on Monday P1 and Science (second teacher) on Tuesday P1;
    section B has Maths (second teacher) on Monday P3."""
    slots = {
        "maths_a": plan.slot(1, 1),
        "science_a": plan.slot(1, 2, assignment=second_teacher.science_a),
        "maths_b": plan.slot(3, 1, section=world.section_b, assignment=second_teacher.maths_b),
        "assembly": plan.slot(3, 2, kind="activity", title="Assembly"),
    }
    plan.publish()
    return slots


def _record(api, slot, date=MONDAY, expect=201, **extra):
    return api.post("lessons", {"slot_id": slot["id"], "date": str(date), **extra}, expect=expect)


# ------------------------------------------------------------------------------------------------ writing
def test_a_teacher_records_their_own_lesson(world, live, api_as):
    teacher = api_as(world.teacher)
    lesson = _record(teacher, live["maths_a"], topic="Fractions")
    assert (lesson["status"], lesson["topic"]) == ("held", "Fractions")
    assert lesson["teacher"]["id"] == str(world.teacher_staff.pk)
    assert Lesson.objects.get(pk=lesson["id"]).recorded_by_id == world.teacher.pk
    assert _record(teacher, live["maths_a"], expect=409)["error"]["code"] == "conflict"
    updated = teacher.patch(f"lessons/{lesson['id']}", {"topic": "Fractions II"})
    assert updated["topic"] == "Fractions II"
    assert AuditEvent.objects.filter(action="timetable.lesson.recorded", target_id=lesson["id"]).exists()


def test_a_teacher_cannot_record_someone_elses_class(world, live, api_as):
    teacher = api_as(world.teacher)
    _record(teacher, live["science_a"], date=TUESDAY, expect=404)  # their section, another teacher's slot
    _record(teacher, live["maths_b"], expect=404)
    other = _record(api_as(world.admin), live["science_a"], date=TUESDAY)
    teacher.patch(f"lessons/{other['id']}", {"topic": "Mine now"}, expect=404)


def test_a_teacher_who_no_longer_teaches_the_class_cannot_record_it(world, live, api_as):
    teacher = api_as(world.teacher)
    lesson = _record(teacher, live["maths_a"])
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(status="ended")
    teacher.patch(f"lessons/{lesson['id']}", {"topic": "Late edit"}, expect=403)
    _record(teacher, live["maths_a"], date=MONDAY + datetime.timedelta(days=7), expect=403)


@pytest.mark.parametrize("member", ["student_member", "parent"])
def test_students_and_parents_cannot_record_lessons(world, live, api_as, member):
    _record(api_as(getattr(world, member)), live["maths_a"], expect=403)


def test_the_school_records_any_lesson_in_the_teachers_name(world, live, admin_api):
    lesson = _record(admin_api, live["maths_b"], status="cancelled", cancellation_reason="Staff meeting")
    assert lesson["teacher"]["full_name"]
    assert str(Lesson.objects.get(pk=lesson["id"]).staff_id) == live["maths_b"]["teacher"]["id"]


def test_lessons_follow_the_timetable(world, live, plan, admin_api, new_plan):
    def error(slot, date, **extra):
        return _record(admin_api, slot, date=date, expect=400, **extra)["error"]["fields"]

    assert "date" in error(live["maths_a"], TUESDAY)  # Monday slot
    assert "date" in error(live["maths_a"], datetime.date(2027, 4, 5))  # after the timetable
    assert "slot_id" in error(live["assembly"], TUESDAY)  # an activity
    assert "cancellation_reason" in error(live["maths_a"], MONDAY, cancellation_reason="Rain")
    draft = new_plan("Draft")
    draft.period(1, "09:00", "09:45")
    slot = draft.slot(1)
    assert "slot_id" in error(slot, MONDAY)


def test_future_lessons_can_be_cancelled_but_not_held(world, live, api_as, time_machine):
    time_machine.move_to(datetime.datetime(2026, 7, 14, 9, 0, tzinfo=datetime.UTC))
    admin_api = api_as(world.admin)  # signed in at the new time
    next_monday = MONDAY + datetime.timedelta(days=7)
    assert "status" in _record(admin_api, live["maths_a"], date=next_monday, expect=400)["error"]["fields"]
    cancelled = _record(
        admin_api, live["maths_a"], date=next_monday, status="cancelled", cancellation_reason="Holiday"
    )
    held = admin_api.patch(f"lessons/{cancelled['id']}", {"status": "held"}, expect=400)
    assert "status" in held["error"]["fields"]


def test_status_changes_keep_the_reason_consistent(world, live, admin_api):
    lesson = _record(admin_api, live["maths_a"], status="cancelled", cancellation_reason="Strike")
    held = admin_api.patch(f"lessons/{lesson['id']}", {"status": "held"})
    assert (held["status"], held["cancellation_reason"]) == ("held", "")
    bad = admin_api.patch(f"lessons/{lesson['id']}", {"cancellation_reason": "Rain"}, expect=400)
    assert "cancellation_reason" in bad["error"]["fields"]
    again = admin_api.patch(f"lessons/{lesson['id']}", {"status": "cancelled", "cancellation_reason": "Rain"})
    assert again["cancellation_reason"] == "Rain"


def test_lessons_of_a_closed_year_are_history(world, live, admin_api):
    lesson = _record(admin_api, live["maths_a"])
    AcademicYear.objects.filter(pk=world.year.pk).update(status="closed", is_current=False)
    admin_api.patch(f"lessons/{lesson['id']}", {"topic": "Late"}, expect=409)
    _record(admin_api, live["maths_a"], date=MONDAY + datetime.timedelta(days=7), expect=409)


def test_lessons_cannot_be_deleted(world, live, admin_api):
    lesson = _record(admin_api, live["maths_a"])
    assert admin_api.client.delete(f"/api/v1/lessons/{lesson['id']}").status_code == 403  # no such permission
    assert Lesson.objects.filter(pk=lesson["id"]).exists()


# ------------------------------------------------------------------------------------------------ reading
def _lesson_ids(api):
    return {row["id"] for row in api.get("lessons")["results"]}


def test_who_reads_which_lessons(world, live, admin_api, api_as, make_member, second_teacher):
    in_a = _record(admin_api, live["maths_a"])["id"]
    science = _record(admin_api, live["science_a"], date=TUESDAY)["id"]
    in_b = _record(admin_api, live["maths_b"])["id"]
    assert _lesson_ids(admin_api) == {in_a, science, in_b}
    assert _lesson_ids(api_as(world.teacher)) == {in_a, science}  # own + the section they teach
    assert _lesson_ids(api_as(second_teacher.member)) == {in_a, science, in_b}  # teaches A and B
    assert _lesson_ids(api_as(world.student_member)) == {in_a, science}
    assert _lesson_ids(api_as(world.parent)) == {in_a, science}
    assert _lesson_ids(api_as(make_member(world.school, roles=["teacher"]))) == set()
    api_as(world.parent).get(f"lessons/{in_b}", expect=404)
    filtered = admin_api.get(f"lessons?section_id={world.section_b.pk}&date_from={MONDAY}&date_to={MONDAY}")
    assert [row["id"] for row in filtered["results"]] == [in_b]
    for role in ("accountant", "librarian"):
        api_as(make_member(world.school, roles=[role])).get("lessons", expect=403)


def test_recorded_lessons_appear_in_schedules(world, live, api_as):
    teacher = api_as(world.teacher)
    lesson = _record(teacher, live["maths_a"], topic="Decimals")
    entries = week(api_as(world.student_member), "schedule/me")
    monday = next(e for e in entries if e["slot_id"] == live["maths_a"]["id"])
    assert monday["lesson"] == {"id": lesson["id"], "status": "held", "topic": "Decimals"}
    tuesday = next(e for e in entries if e["slot_id"] == live["science_a"]["id"])
    assert tuesday["lesson"] is None
