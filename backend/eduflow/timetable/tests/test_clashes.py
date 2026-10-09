"""Clash rejection: by the services (precise 409 messages) and by the database (the guarantee).

The database tests write rows with the ORM, past every service check, to show that the constraints alone
refuse double-booking and inconsistent copies.
"""

import datetime
import threading

import pytest
from django.db import IntegrityError, connection, transaction

from eduflow.academics.models import Room
from eduflow.people.models import TeacherAssignment
from eduflow.timetable import services
from eduflow.timetable.models import Period, Timetable, TimetableSlot

pytestmark = pytest.mark.django_db


@pytest.fixture
def maths_b(world):
    """The world's teacher also teaches Mathematics to section B."""
    return TeacherAssignment.objects.create(
        school=world.school,
        staff=world.teacher_staff,
        academic_year=world.year,
        section=world.section_b,
        subject=world.subject,
    )


@pytest.fixture
def room(world):
    return Room.objects.create(school=world.school, name="Room 1", code="r1")


# ------------------------------------------------------------------------------------------- one timetable
def test_a_section_has_one_slot_per_period_and_day(world, plan, second_teacher):
    plan.slot(1, 1)
    clash = plan.slot(1, 1, assignment=second_teacher.science_a, expect=409)
    assert "Section 5a" in clash["error"]["message"]
    plan.slot(1, 2, assignment=second_teacher.science_a)  # another day
    plan.slot(3, 1, assignment=second_teacher.science_a)  # another period


def test_a_teacher_is_never_in_two_sections_at_once(world, plan, maths_b):
    plan.slot(1, 1)
    clash = plan.slot(1, 1, section=world.section_b, assignment=maths_b, expect=409)
    assert world.teacher.user.full_name in clash["error"]["message"]
    assert "Monday 09:00-09:45" in clash["error"]["message"]


def test_a_room_is_never_double_booked(world, plan, room, second_teacher):
    plan.slot(1, 1, room=room)
    clash = plan.slot(1, 1, section=world.section_b, assignment=second_teacher.maths_b, room=room, expect=409)
    assert "Room r1" in clash["error"]["message"]


def test_moving_a_slot_onto_a_taken_place_is_refused(world, plan, admin_api, maths_b):
    plan.slot(1, 1)
    other = plan.slot(3, 1, section=world.section_b, assignment=maths_b)
    admin_api.patch(f"timetable-slots/{other['id']}", {"period_id": plan.periods[1]}, expect=409)
    assert str(TimetableSlot.objects.get(pk=other["id"]).period_id) == plan.periods[3]


# --------------------------------------------------------------------------------- across live timetables
def _wing(new_plan, name, start, end, **dates):
    """A second timetable with different bell times (a separate wing of the school)."""
    wing = new_plan(name, **dates)
    wing.period(1, start, end)
    return wing


def test_published_timetables_with_different_bells_cannot_double_book_a_teacher(
    world, plan, new_plan, maths_b
):
    plan.slot(1, 1)  # Monday 09:00-09:45
    plan.publish()
    wing = _wing(new_plan, "Senior wing", "09:30", "10:15")
    wing.slot(1, 1, section=world.section_b, assignment=maths_b)  # fine while a draft
    clash = wing.publish(expect=409)
    assert "Main" in clash["error"]["message"]
    assert Timetable.objects.get(pk=wing.id).status == "draft"


def test_back_to_back_and_other_days_do_not_clash(world, plan, new_plan, maths_b):
    plan.slot(1, 1)  # 09:00-09:45
    plan.publish()
    wing = _wing(new_plan, "Senior wing", "09:45", "10:30")
    wing.slot(1, 1, section=world.section_b, assignment=maths_b)
    wing.slot(1, 2, assignment=world.assignment)
    wing.publish()


def test_timetables_with_separate_dates_do_not_clash(world, plan, new_plan, admin_api):
    plan.slot(1, 1)
    plan.publish()
    admin_api.patch(f"timetables/{plan.id}", {"effective_to": "2026-09-30"})
    term2 = _wing(new_plan, "Term 2", "09:00", "09:45", effective_from="2026-10-01")
    term2.slot(1, 1)  # same section, teacher and time: a later term
    term2.publish()
    # Stretching the first timetable back over the second is refused, and nothing changes.
    admin_api.patch(f"timetables/{plan.id}", {"effective_to": "2026-10-15"}, expect=409)
    assert Timetable.objects.get(pk=plan.id).effective_to == datetime.date(2026, 9, 30)


def test_adding_to_a_live_timetable_checks_other_live_ones(world, plan, new_plan, maths_b):
    plan.slot(1, 1)
    plan.publish()
    wing = _wing(new_plan, "Senior wing", "11:00", "11:45")
    wing.slot(1, 1, section=world.section_b, assignment=maths_b)
    wing.publish()
    wing.period(2, "09:15", "10:00")
    clash = wing.slot(2, 1, section=world.section_b, assignment=maths_b, expect=409)
    assert "Main" in clash["error"]["message"]


def test_changing_bell_times_of_a_live_timetable_rechecks_clashes(world, plan, new_plan, admin_api, maths_b):
    plan.slot(1, 1)
    plan.publish()
    wing = _wing(new_plan, "Senior wing", "11:00", "11:45")
    wing.slot(1, 1, section=world.section_b, assignment=maths_b)
    wing.publish()
    admin_api.patch(
        f"timetable-periods/{wing.periods[1]}", {"start_time": "09:30", "end_time": "10:15"}, expect=409
    )
    assert Period.objects.get(pk=wing.periods[1]).start_time == datetime.time(11, 0)


def test_the_clash_message_reports_a_few_and_counts_the_rest(world, plan, new_plan, second_teacher):
    for weekday in range(1, 6):
        plan.slot(1, weekday)
    plan.publish()
    wing = _wing(new_plan, "Copy", "09:00", "09:45")
    for weekday in range(1, 6):  # section A is already taken at that time every day
        wing.slot(1, weekday, section=world.section_a, assignment=second_teacher.science_a)
    message = wing.publish(expect=409)["error"]["message"]
    assert message.count("Section 5a") == 3
    assert "(and 2 more)" in message


# ------------------------------------------------------------------------------------------- the database
def _orm_slot(timetable, period, section, assignment=None, **override):
    values = {
        "school": timetable.school,
        "timetable": timetable,
        "period": period,
        "weekday": 1,
        "section": section,
        "kind": "lesson" if assignment else "activity",
        "title": "" if assignment else "Assembly",
        "assignment": assignment,
        "staff_id": assignment.staff_id if assignment else None,
        "subject_id": assignment.subject_id if assignment else None,
        "academic_year": timetable.academic_year,
        "effective_from": timetable.effective_from,
        "effective_to": timetable.effective_to,
        "is_live": timetable.is_live,
        "start_time": period.start_time,
        "end_time": period.end_time,
    }
    values.update(override)
    return TimetableSlot.objects.create(**values)


def _orm_timetable(world, name, *, live, start="09:00", end="09:45"):
    timetable = Timetable.objects.create(
        school=world.school,
        academic_year=world.year,
        name=name,
        effective_from=world.year.start_date,
        effective_to=world.year.end_date,
        status="published" if live else "draft",
        is_live=live,
        published_at=datetime.datetime.now(datetime.UTC) if live else None,
    )
    period = Period.objects.create(
        school=world.school,
        timetable=timetable,
        number=1,
        name="P1",
        start_time=datetime.time.fromisoformat(start),
        end_time=datetime.time.fromisoformat(end),
    )
    return timetable, period


def test_database_refuses_a_teacher_in_two_live_timetables_at_once(world, maths_b):
    first, p1 = _orm_timetable(world, "A", live=True)
    second, p2 = _orm_timetable(world, "B", live=True, start="09:30", end="10:15")
    _orm_slot(first, p1, world.section_a, world.assignment)
    with pytest.raises(IntegrityError, match="timetable_slot_live_staff_excl"), transaction.atomic():
        _orm_slot(second, p2, world.section_b, maths_b)


def test_database_refuses_a_double_booked_room_and_section(world, room, second_teacher):
    first, p1 = _orm_timetable(world, "A", live=True)
    second, p2 = _orm_timetable(world, "B", live=True, start="09:30", end="10:15")
    _orm_slot(first, p1, world.section_a, world.assignment, room=room)
    with pytest.raises(IntegrityError, match="timetable_slot_live_room_excl"), transaction.atomic():
        _orm_slot(second, p2, world.section_b, second_teacher.maths_b, room=room)
    with pytest.raises(IntegrityError, match="timetable_slot_live_section_excl"), transaction.atomic():
        _orm_slot(second, p2, world.section_a, second_teacher.science_a)


def test_database_ignores_drafts_and_publishing_brings_the_check(world, maths_b):
    live, p1 = _orm_timetable(world, "A", live=True)
    draft, p2 = _orm_timetable(world, "B", live=False, start="09:30", end="10:15")
    _orm_slot(live, p1, world.section_a, world.assignment)
    _orm_slot(draft, p2, world.section_b, maths_b)  # drafts are free
    with pytest.raises(IntegrityError, match="timetable_slot_live_staff_excl"), transaction.atomic():
        # The live flag cascades to the slot, which then meets the constraint.
        Timetable.objects.filter(pk=draft.pk).update(
            status="published", is_live=True, published_at=datetime.datetime.now(datetime.UTC)
        )


def test_database_keeps_slot_copies_equal_to_their_sources(world):
    timetable, period = _orm_timetable(world, "A", live=False)
    with pytest.raises(IntegrityError, match="timetable_slot_period_copy_fk"), transaction.atomic():
        _orm_slot(timetable, period, world.section_a, world.assignment, start_time=datetime.time(8, 0))
    with pytest.raises(IntegrityError, match="timetable_slot_timetable_copy_fk"), transaction.atomic():
        _orm_slot(timetable, period, world.section_a, world.assignment, is_live=True)
    slot = _orm_slot(timetable, period, world.section_a, world.assignment)
    Period.objects.filter(pk=period.pk).update(start_time=datetime.time(8, 0), end_time=datetime.time(8, 45))
    Timetable.objects.filter(pk=timetable.pk).update(effective_to=datetime.date(2026, 12, 31))
    slot.refresh_from_db()
    assert (slot.start_time, slot.effective_to) == (datetime.time(8, 0), datetime.date(2026, 12, 31))


def test_database_keeps_a_slot_teacher_equal_to_its_assignment(world, second_teacher):
    timetable, period = _orm_timetable(world, "A", live=False)
    with pytest.raises(IntegrityError, match="timetable_slot_assignment_key_fk"), transaction.atomic():
        _orm_slot(timetable, period, world.section_a, world.assignment, staff_id=second_teacher.staff.pk)
    with pytest.raises(IntegrityError, match="timetable_slot_assignment_key_fk"), transaction.atomic():
        _orm_slot(timetable, period, world.section_b, world.assignment)  # assignment of section A
    with pytest.raises(IntegrityError, match="timetable_slot_kind_check"), transaction.atomic():
        _orm_slot(timetable, period, world.section_a, None, kind="lesson")


def test_database_refuses_overlapping_periods_and_a_section_from_another_year(world, other_world):
    timetable, _ = _orm_timetable(world, "A", live=False)
    with pytest.raises(IntegrityError, match="timetable_period_no_overlap"), transaction.atomic():
        Period.objects.create(
            school=world.school,
            timetable=timetable,
            number=2,
            name="P2",
            start_time=datetime.time(9, 30),
            end_time=datetime.time(10, 0),
        )
    period = Period.objects.get(timetable=timetable)
    with pytest.raises(IntegrityError), transaction.atomic():
        _orm_slot(timetable, period, other_world.section_a, None)


# ------------------------------------------------------------------------------------------- concurrency
@pytest.mark.django_db(transaction=True)
def test_parallel_publishing_of_clashing_timetables_lets_one_win(world, new_plan, maths_b):
    first = _wing(new_plan, "A", "09:00", "09:45")
    first.slot(1, 1)
    second = _wing(new_plan, "B", "09:30", "10:15")
    second.slot(1, 1, section=world.section_b, assignment=maths_b)
    actor = _actor(world)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def publish(timetable_id: str) -> None:
        try:
            barrier.wait()
            services.publish_timetable(actor, Timetable.objects.get(pk=timetable_id))
            outcomes.append("published")
        except Exception as exc:  # each thread reports what stopped it
            outcomes.append(type(exc).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=publish, args=(tid,)) for tid in (first.id, second.id)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["Conflict", "published"], outcomes
    assert Timetable.objects.filter(status="published").count() == 1


def _actor(world):
    from eduflow.authz.grants import Actor

    return Actor(user=world.admin.user, school=world.school, membership=world.admin)
