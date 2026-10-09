"""Schedules: whose schedule each role may see, and what goes into it."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from eduflow.people.models import Enrollment, TeacherAssignment
from eduflow.people.services import transfer_enrollment

from .conftest import MONDAY, week

pytestmark = pytest.mark.django_db


@pytest.fixture
def live(world, plan, second_teacher):
    """Section A: Maths (world teacher) Mon P1, Science (second teacher) Tue P1, Assembly Wed P3.
    Section B: Maths (second teacher) Mon P3."""
    plan.slot(1, 1)
    plan.slot(1, 2, assignment=second_teacher.science_a)
    plan.slot(3, 3, kind="activity", title="Assembly")
    plan.slot(3, 1, section=world.section_b, assignment=second_teacher.maths_b)
    plan.publish()
    return plan


def _summary(entries):
    return [
        (e["date"], e["period"]["number"], e["section"]["code"], e["subject"] and e["subject"]["name"])
        for e in entries
    ]


# ------------------------------------------------------------------------------------------------ content
def test_a_teacher_sees_what_they_teach(world, live, api_as):
    entries = week(api_as(world.teacher), "schedule/me")
    assert _summary(entries) == [(str(MONDAY), 1, "5a", "Mathematics")]
    entry = entries[0]
    assert entry["role"] == "teacher"
    assert entry["teacher"]["id"] == str(world.teacher_staff.pk)
    assert (entry["period"]["start_time"], entry["period"]["end_time"]) == ("09:00:00", "09:45:00")
    assert entry["weekday"] == 1


def test_a_student_sees_their_sections_week(world, live, api_as):
    entries = week(api_as(world.student_member), "schedule/me")
    assert [(e["weekday"], e["kind"], e["role"]) for e in entries] == [
        (1, "lesson", "student"),
        (2, "lesson", "student"),
        (3, "activity", "student"),
    ]
    assert entries[2]["title"] == "Assembly"
    assert entries[2]["teacher"] is None


def test_a_parent_sees_their_childs_schedule_only(world, live, api_as):
    parent = api_as(world.parent)
    assert len(week(parent, f"students/{world.student.pk}/schedule")) == 3
    parent.get(f"students/{world.other_student.pk}/schedule", expect=404)
    parent.get(f"sections/{world.section_b.pk}/schedule", expect=404)
    parent.get(f"staff/{world.teacher_staff.pk}/schedule", expect=403)  # no staff.read at all
    assert week(parent, "schedule/me") == []  # a parent neither teaches nor attends


def test_a_teacher_sees_the_schedules_of_classes_they_teach(world, live, api_as, second_teacher):
    teacher = api_as(world.teacher)
    assert len(week(teacher, f"sections/{world.section_a.pk}/schedule")) == 3
    assert len(week(teacher, f"students/{world.student.pk}/schedule")) == 3
    teacher.get(f"sections/{world.section_b.pk}/schedule", expect=404)
    teacher.get(f"students/{world.other_student.pk}/schedule", expect=404)
    teacher.get(f"staff/{second_teacher.staff.pk}/schedule", expect=404)  # staff.read: self only
    assert len(week(teacher, f"staff/{world.teacher_staff.pk}/schedule")) == 1


def test_the_school_sees_every_schedule(world, live, admin_api, second_teacher):
    assert len(week(admin_api, f"staff/{second_teacher.staff.pk}/schedule")) == 2
    assert len(week(admin_api, f"sections/{world.section_b.pk}/schedule")) == 1
    assert len(week(admin_api, f"students/{world.other_student.pk}/schedule")) == 1


def test_a_class_whose_teacher_left_shows_no_teacher(world, live, api_as):
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(status="ended")
    assert week(api_as(world.teacher), "schedule/me") == []
    monday = week(api_as(world.student_member), "schedule/me")[0]
    assert (monday["subject"]["name"], monday["teacher"]) == ("Mathematics", None)


def test_a_transfer_switches_sections_mid_week(world, live, api_as):
    wednesday = MONDAY + datetime.timedelta(days=2)
    from eduflow.authz.grants import Actor

    actor = Actor(user=world.admin.user, school=world.school, membership=world.admin)
    enrollment = Enrollment.objects.select_related("academic_year").get(pk=world.enrollment.pk)
    transfer_enrollment(actor, enrollment, section_id=world.section_b.pk, date=wednesday)
    entries = week(api_as(world.parent), f"students/{world.student.pk}/schedule", start=MONDAY, days=14)
    first_week = [
        (e["date"], e["section"]["code"])
        for e in entries
        if e["date"] < str(MONDAY + datetime.timedelta(days=7))
    ]
    assert first_week == [(str(MONDAY), "5a"), (str(MONDAY + datetime.timedelta(days=1)), "5a")]
    assert {e["section"]["code"] for e in entries if e["date"] >= str(wednesday)} == {"5b"}


def test_schedules_end_with_the_timetable(world, live, admin_api):
    admin_api.patch(f"timetables/{live.id}", {"effective_to": str(MONDAY + datetime.timedelta(days=1))})
    entries = week(admin_api, f"sections/{world.section_a.pk}/schedule", days=14)
    assert [e["weekday"] for e in entries] == [1, 2]


# ------------------------------------------------------------------------------------------------ the window
def test_the_default_window_is_the_current_week(world, live, api_as, time_machine):
    time_machine.move_to(datetime.datetime(2026, 7, 15, 6, 0, tzinfo=datetime.UTC))  # a Wednesday
    data = api_as(world.student_member).get("schedule/me")
    assert (data["date_from"], data["date_to"]) == (str(MONDAY), str(MONDAY + datetime.timedelta(days=6)))
    assert len(data["entries"]) == 3


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("date_from=2026-07-13&date_to=2026-08-31", "date_to"),
        ("date_from=2026-07-13&date_to=2026-07-12", "date_to"),
        ("date_from=13-07-2026", "date_from"),
        ("date_from=2026-07-13&unknown=1", "unknown"),
    ],
)
def test_bad_windows_are_refused(world, live, api_as, query, field):
    response = api_as(world.student_member).get(f"schedule/me?{query}", expect=400)
    assert field in response["error"]["fields"]


def test_a_42_day_window_is_allowed(world, live, api_as):
    entries = week(api_as(world.student_member), "schedule/me", days=42)
    assert len(entries) == 18


# ------------------------------------------------------------------------------------------------ queries
def test_schedule_queries_do_not_grow_with_the_timetable(world, plan, api_as, second_teacher, new_plan):
    plan.slot(1, 1)
    plan.publish()
    student = api_as(world.student_member)
    path = f"schedule/me?date_from={MONDAY}&date_to={MONDAY + datetime.timedelta(days=13)}"
    student.get(path)
    with CaptureQueriesContext(connection) as small:
        student.get(path)
    extra = new_plan("Afternoon", effective_from="2026-06-01")
    for number, start in enumerate(("12:00", "13:00", "14:00"), start=1):
        extra.period(number, start, start.replace(":00", ":45"))
        for weekday in range(1, 6):
            extra.slot(number, weekday, assignment=second_teacher.science_a)
    extra.publish()
    with CaptureQueriesContext(connection) as large:
        assert len(student.get(path)["entries"]) == 2 + 30
    assert len(large) == len(small)
