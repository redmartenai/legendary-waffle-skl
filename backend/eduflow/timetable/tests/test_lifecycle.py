"""Timetable lifecycle: draft -> published -> archived, periods, slots, copies, and what each state allows."""

import datetime

import pytest

from eduflow.academics.models import AcademicYear, Room, Section, Term
from eduflow.audit.models import AuditEvent
from eduflow.people.models import TeacherAssignment
from eduflow.timetable.models import Timetable, TimetableSlot

from .conftest import MONDAY, week

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------------------------------------ timetables
def test_dates_default_to_the_year_or_the_term(world, new_plan, admin_api):
    whole = admin_api.get(f"timetables/{new_plan('Year').id}")
    assert (whole["effective_from"], whole["effective_to"]) == ("2026-06-01", "2027-03-31")
    assert whole["status"] == "draft"
    term = Term.objects.create(
        school=world.school,
        academic_year=world.year,
        name="Term 1",
        code="t1",
        start_date=datetime.date(2026, 6, 1),
        end_date=datetime.date(2026, 9, 30),
    )
    by_term = admin_api.get(f"timetables/{new_plan('Term', term_id=str(term.pk)).id}")
    assert (by_term["effective_to"], by_term["term"]["id"]) == ("2026-09-30", str(term.pk))


def test_timetable_validation(world, other_world, admin_api, new_plan):
    year = str(world.year.pk)
    outside = admin_api.post(
        "timetables", {"academic_year_id": year, "name": "X", "effective_to": "2027-05-01"}, expect=400
    )
    assert "effective_from" in outside["error"]["fields"]
    backwards = {
        "academic_year_id": year,
        "name": "Y",
        "effective_from": "2026-08-01",
        "effective_to": "2026-07-01",
    }
    assert "effective_to" in admin_api.post("timetables", backwards, expect=400)["error"]["fields"]
    foreign = admin_api.post(
        "timetables", {"academic_year_id": str(other_world.year.pk), "name": "Z"}, expect=400
    )
    assert "academic_year_id" in foreign["error"]["fields"]
    new_plan("Main")
    admin_api.post("timetables", {"academic_year_id": year, "name": "MAIN"}, expect=409)
    other_year = AcademicYear.objects.create(
        school=world.school,
        name="2027-28",
        start_date=datetime.date(2027, 6, 1),
        end_date=datetime.date(2028, 3, 31),
    )
    term = Term.objects.create(
        school=world.school,
        academic_year=other_year,
        name="T",
        code="t",
        start_date=datetime.date(2027, 6, 1),
        end_date=datetime.date(2027, 9, 30),
    )
    wrong_term = admin_api.post(
        "timetables", {"academic_year_id": year, "name": "W", "term_id": str(term.pk)}, expect=400
    )
    assert "term_id" in wrong_term["error"]["fields"]


def test_publish_needs_slots_and_current_assignments(world, plan):
    assert "non_field_errors" in plan.publish(expect=400)["error"]["fields"]
    plan.slot(1)
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(status="ended")
    assert "ended" in plan.publish(expect=400)["error"]["fields"]["non_field_errors"][0]
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(status="active")
    published = plan.publish()
    assert published["status"] == "published"
    assert published["published_at"]
    plan.publish(expect=409)  # only drafts


def test_only_published_timetables_produce_schedules(world, plan, api_as):
    plan.slot(1)
    teacher = api_as(world.teacher)
    assert week(teacher, "schedule/me") == []  # draft
    plan.publish()
    assert len(week(teacher, "schedule/me")) == 1
    plan.archive()
    assert week(teacher, "schedule/me") == []  # archived
    assert Timetable.objects.get(pk=plan.id).archived_at is not None
    plan.archive(expect=409)


def test_archived_timetables_and_closed_years_are_read_only(world, plan, admin_api):
    slot = plan.slot(1)
    plan.publish()
    plan.archive()
    admin_api.patch(f"timetables/{plan.id}", {"name": "New"}, expect=409)
    admin_api.post(
        "timetable-periods",
        {"timetable_id": plan.id, "number": 9, "name": "P9", "start_time": "14:00", "end_time": "14:45"},
        expect=409,
    )
    admin_api.patch(f"timetable-slots/{slot['id']}", {"weekday": 2}, expect=409)
    admin_api.delete(f"timetable-slots/{slot['id']}", expect=409)

    draft = plan.api.post("timetables", {"academic_year_id": str(world.year.pk), "name": "Next"})
    AcademicYear.objects.filter(pk=world.year.pk).update(status="closed", is_current=False)
    admin_api.patch(f"timetables/{draft['id']}", {"name": "X"}, expect=409)
    admin_api.post(f"timetables/{draft['id']}/publish", expect=409)


def test_only_drafts_can_be_deleted(world, plan, admin_api, new_plan):
    plan.slot(1)
    plan.publish()
    admin_api.delete(f"timetables/{plan.id}", expect=409)
    draft = new_plan("Draft", effective_from="2026-10-01")
    draft.period(1, "09:00", "09:45")
    draft.slot(1)
    admin_api.delete(f"timetables/{draft.id}")
    assert not TimetableSlot.objects.filter(timetable_id=draft.id).exists()
    assert AuditEvent.objects.filter(action="timetable.timetable.deleted", target_id=draft.id).exists()


def test_term_of_a_published_timetable_is_fixed(world, plan, admin_api):
    term = Term.objects.create(
        school=world.school,
        academic_year=world.year,
        name="T",
        code="t",
        start_date=datetime.date(2026, 6, 1),
        end_date=datetime.date(2026, 9, 30),
    )
    admin_api.patch(f"timetables/{plan.id}", {"term_id": str(term.pk), "effective_to": "2026-09-30"})
    plan.slot(1)
    plan.publish()
    assert (
        "term_id"
        in admin_api.patch(f"timetables/{plan.id}", {"term_id": None}, expect=400)["error"]["fields"]
    )
    outside = admin_api.patch(f"timetables/{plan.id}", {"effective_to": "2026-10-31"}, expect=400)
    assert "effective_from" in outside["error"]["fields"]


def test_copy_makes_a_draft_without_ended_assignments(world, plan, admin_api, second_teacher):
    plan.slot(1, 1)
    plan.slot(1, 2, assignment=second_teacher.science_a)
    plan.slot(3, 1, kind="activity", title="Library")
    plan.publish()
    TeacherAssignment.objects.filter(pk=second_teacher.science_a.pk).update(status="ended")
    archived_room = Room.objects.create(school=world.school, name="Old", code="old")
    TimetableSlot.objects.filter(timetable_id=plan.id, kind="activity").update(room=archived_room)
    Room.objects.filter(pk=archived_room.pk).update(status="archived")
    copy = admin_api.post(f"timetables/{plan.id}/copy", {"name": "Term 2", "effective_from": "2026-10-01"})
    assert (copy["status"], copy["effective_from"]) == ("draft", "2026-10-01")
    slots = admin_api.get(f"timetable-slots?timetable_id={copy['id']}")["results"]
    assert sorted((s["weekday"], s["kind"]) for s in slots) == [(1, "activity"), (1, "lesson")]
    assert all(s["room"] is None for s in slots)
    periods = admin_api.get(f"timetable-periods?timetable_id={copy['id']}")["results"]
    assert sorted(p["number"] for p in periods) == [1, 2, 3]
    event = AuditEvent.objects.get(action="timetable.timetable.copied", target_id=copy["id"])
    assert (event.metadata["slots"], event.metadata["skipped"]) == (2, 1)


# ------------------------------------------------------------------------------------------------ periods
def test_period_rules(world, plan, admin_api):
    body = {"timetable_id": plan.id, "number": 4, "name": "P4", "start_time": "10:30", "end_time": "11:15"}
    assert "start_time" in admin_api.post("timetable-periods", body, expect=400)["error"]["fields"]
    body.update(start_time="11:00", end_time="10:50")
    assert "end_time" in admin_api.post("timetable-periods", body, expect=400)["error"]["fields"]
    body.update(number=1, start_time="11:00", end_time="11:45")
    admin_api.post("timetable-periods", body, expect=409)  # number taken
    body.update(number=4)
    period = admin_api.post("timetable-periods", body)
    assert period["timetable_id"] == plan.id
    plan.periods[4] = period["id"]
    plan.slot(4)
    assert (
        "is_break"
        in admin_api.patch(f"timetable-periods/{period['id']}", {"is_break": True}, expect=400)["error"][
            "fields"
        ]
    )
    admin_api.delete(f"timetable-periods/{period['id']}", expect=409)  # has a slot
    admin_api.delete(f"timetable-periods/{plan.periods[3]}")


def test_period_time_change_moves_its_slots(world, plan, admin_api):
    slot = plan.slot(3)
    admin_api.patch(f"timetable-periods/{plan.periods[3]}", {"start_time": "10:05", "end_time": "10:50"})
    moved = TimetableSlot.objects.get(pk=slot["id"])
    assert (moved.start_time, moved.end_time) == (datetime.time(10, 5), datetime.time(10, 50))
    assert admin_api.get(f"timetable-slots/{slot['id']}")["period"]["start_time"] == "10:05:00"


# ------------------------------------------------------------------------------------------------ slots
def test_slot_validation(world, other_world, plan, admin_api, second_teacher):
    def error(**kw):
        return plan.slot(expect=400, **kw)["error"]["fields"]

    assert "period_id" in error(number=2)  # a break
    assert "assignment_id" in error(number=1, assignment=second_teacher.maths_b)  # another section's
    assert "section_id" in error(number=1, section=other_world.section_a)
    assert "title" in error(number=1, kind="activity")
    archived = Section.objects.create(
        school=world.school,
        academic_year=world.year,
        grade=world.grade,
        name="C",
        code="5c",
        status="archived",
    )
    assert "section_id" in error(number=1, section=archived)
    homeroom = TeacherAssignment.objects.create(
        school=world.school,
        staff=world.teacher_staff,
        academic_year=world.year,
        section=world.section_b,
        is_class_teacher=True,
    )
    assert "assignment_id" in error(number=1, section=world.section_b, assignment=homeroom)
    elsewhere = Room.objects.create(
        school=world.school, name="Annex", code="annex", campus=_other_campus(world)
    )
    assert "room_id" in error(number=1, room=elsewhere)
    activity_with_teacher = plan.slot(
        1, kind="activity", title="Assembly", assignment=world.assignment, expect=400
    )
    assert "assignment_id" in activity_with_teacher["error"]["fields"]
    assert "weekday" in error(number=1, weekday=8)


def test_lesson_without_assignment_is_refused(world, plan, admin_api):
    body = {
        "timetable_id": plan.id,
        "period_id": plan.periods[1],
        "weekday": 1,
        "section_id": str(world.section_a.pk),
    }
    assert "assignment_id" in admin_api.post("timetable-slots", body, expect=400)["error"]["fields"]


def test_slot_period_from_another_timetable_is_refused(world, plan, new_plan):
    other = new_plan("Other")
    other.period(1, "09:00", "09:45")
    plan.periods[7] = other.periods[1]
    assert "period_id" in plan.slot(7, expect=400)["error"]["fields"]


def test_slot_update_rules(world, plan, admin_api, second_teacher):
    slot = plan.slot(1)
    changed = admin_api.patch(
        f"timetable-slots/{slot['id']}",
        {"weekday": 3, "period_id": plan.periods[3], "assignment_id": str(second_teacher.science_a.pk)},
    )
    assert (changed["weekday"], changed["period"]["number"]) == (3, 3)
    assert changed["teacher"]["id"] == str(second_teacher.staff.pk)
    assert changed["subject"]["name"] == "Science"
    assert admin_api.patch(f"timetable-slots/{slot['id']}", {"assignment_id": None}, expect=400)
    activity = plan.slot(1, kind="activity", title="Assembly")
    assert admin_api.patch(f"timetable-slots/{activity['id']}", {"title": "Prayer"})["title"] == "Prayer"
    admin_api.patch(f"timetable-slots/{activity['id']}", {"title": ""}, expect=400)
    admin_api.patch(
        f"timetable-slots/{activity['id']}", {"assignment_id": str(world.assignment.pk)}, expect=400
    )
    same = admin_api.patch(f"timetable-slots/{slot['id']}", {"weekday": 3})
    assert same["updated_at"] == changed["updated_at"]  # no change, no write


def test_slot_with_lessons_cannot_be_deleted(world, plan, admin_api):
    slot = plan.slot(1)
    plan.publish()
    admin_api.post("lessons", {"slot_id": slot["id"], "date": str(MONDAY)})
    admin_api.delete(f"timetable-slots/{slot['id']}", expect=409)


def test_every_change_is_audited(world, plan, admin_api):
    slot = plan.slot(1)
    admin_api.patch(f"timetable-slots/{slot['id']}", {"weekday": 2})
    plan.publish()
    actions = set(AuditEvent.objects.filter(action__startswith="timetable.").values_list("action", flat=True))
    assert {
        "timetable.timetable.created",
        "timetable.period.created",
        "timetable.slot.created",
        "timetable.slot.updated",
        "timetable.timetable.published",
    } <= actions


def _other_campus(world):
    from eduflow.academics.models import Campus

    return Campus.objects.create(school=world.school, name="North", code="north")
