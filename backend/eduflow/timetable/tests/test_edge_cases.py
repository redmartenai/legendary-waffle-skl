"""Less common refusals and the scope rules no default role exercises (custom roles)."""

import datetime

import pytest

from eduflow.academics.models import AcademicYear, Room, Section, Term
from eduflow.authz.models import MembershipRole, Role
from eduflow.authz.services import _set_grants, bump_rbac_version
from eduflow.people.models import StaffProfile, TeacherAssignment
from eduflow.timetable.models import TimetableSlot

from .conftest import MONDAY, week

pytestmark = pytest.mark.django_db


def _custom(world, make_member, grants, **profile):
    member = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key=f"custom-{member.pk.hex[-12:]}", name="Custom")
    _set_grants(role, grants)
    MembershipRole.objects.create(school=world.school, membership=member, role=role)
    bump_rbac_version(world.school.pk)
    if profile:
        StaffProfile.objects.create(
            school=world.school, membership=member, employee_id=f"C-{member.pk.hex[-12:]}", **profile
        )
    return member


def _next_year(world):
    return AcademicYear.objects.create(
        school=world.school,
        name="2027-28",
        start_date=datetime.date(2027, 6, 1),
        end_date=datetime.date(2028, 3, 31),
    )


# ------------------------------------------------------------------------------------------------ refusals
def test_no_timetable_for_a_closed_year(world, admin_api):
    AcademicYear.objects.filter(pk=world.year.pk).update(status="closed", is_current=False)
    body = {"academic_year_id": str(world.year.pk), "name": "Late"}
    assert "academic_year_id" in admin_api.post("timetables", body, expect=400)["error"]["fields"]


def test_a_draft_cannot_take_a_term_of_another_year(world, plan, admin_api):
    term = Term.objects.create(
        school=world.school,
        academic_year=_next_year(world),
        name="T",
        code="t",
        start_date=datetime.date(2027, 6, 1),
        end_date=datetime.date(2027, 9, 30),
    )
    response = admin_api.patch(f"timetables/{plan.id}", {"term_id": str(term.pk)}, expect=400)
    assert "term_id" in response["error"]["fields"]
    assert admin_api.patch(f"timetables/{plan.id}", {"term_id": None})["term"] is None


def test_slots_refuse_archived_rooms_ended_assignments_and_other_years(world, plan, admin_api):
    room = Room.objects.create(school=world.school, name="Old", code="old", status="archived")
    assert "room_id" in plan.slot(1, room=room, expect=400)["error"]["fields"]
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(status="ended")
    assert "assignment_id" in plan.slot(1, expect=400)["error"]["fields"]
    later = Section.objects.create(
        school=world.school, academic_year=_next_year(world), grade=world.grade, name="A", code="6a"
    )
    activity = plan.slot(1, section=later, kind="activity", title="Assembly", expect=400)
    assert "section_id" in activity["error"]["fields"]


def test_slot_room_can_change_and_a_slot_can_be_deleted(world, plan, admin_api):
    slot = plan.slot(1)
    room = Room.objects.create(school=world.school, name="Lab", code="lab")
    assert (
        admin_api.patch(f"timetable-slots/{slot['id']}", {"room_id": str(room.pk)})["room"]["code"] == "lab"
    )
    assert admin_api.patch(f"timetable-slots/{slot['id']}", {"room_id": None})["room"] is None
    admin_api.delete(f"timetable-slots/{slot['id']}")
    assert not TimetableSlot.objects.filter(pk=slot["id"]).exists()


def test_a_live_timetable_without_slots_can_move_its_dates(world, plan, admin_api):
    slot = plan.slot(1)
    plan.publish()
    admin_api.delete(f"timetable-slots/{slot['id']}")
    moved = admin_api.patch(f"timetables/{plan.id}", {"effective_to": "2026-12-31"})
    assert moved["effective_to"] == "2026-12-31"


def test_a_student_without_enrollments_has_an_empty_schedule(world, plan, api_as):
    plan.slot(1)
    plan.publish()
    world.enrollment.delete()
    assert week(api_as(world.student_member), "schedule/me") == []


# ------------------------------------------------------------------------------------------- custom roles
def test_timetable_actions_need_a_school_wide_grant(world, plan, api_as, make_member):
    clerk = _custom(world, make_member, {"timetable.manage": ["section"], "timetable.read": ["school"]})
    plan.slot(1)
    api_as(clerk).post(f"timetables/{plan.id}/publish", expect=403)


def test_section_and_campus_scopes_over_slots_and_lessons(
    world, plan, admin_api, api_as, make_member, second_teacher
):
    in_a = plan.slot(1, 1)["id"]
    in_b = plan.slot(3, 1, section=world.section_b, assignment=second_teacher.maths_b)["id"]
    plan.publish()
    lesson_a = admin_api.post("lessons", {"slot_id": in_a, "date": str(MONDAY)})["id"]
    admin_api.post("lessons", {"slot_id": in_b, "date": str(MONDAY)})

    def ids(member, path):
        return {row["id"] for row in api_as(member).get(path)["results"]}

    teacher = _custom(world, make_member, {"timetable.read": ["section"], "lesson.read": ["section"]})
    TeacherAssignment.objects.create(
        school=world.school,
        staff=StaffProfile.objects.create(school=world.school, membership=teacher, employee_id="C-T"),
        academic_year=world.year,
        section=world.section_a,
        is_class_teacher=True,
    )
    assert ids(teacher, "timetable-slots") == {in_a}
    assert ids(teacher, "lessons") == {lesson_a}

    on_campus = _custom(
        world, make_member, {"timetable.read": ["campus"], "lesson.read": ["campus"]}, campus=world.campus
    )
    assert ids(on_campus, "timetable-slots") == {in_a}  # section A is on the main campus; B has none
    assert ids(on_campus, "lessons") == {lesson_a}
