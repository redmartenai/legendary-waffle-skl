"""Taking a class register: the full-replacement submission, idempotent retries, the lock, and the roster."""

import datetime
import threading

import pytest
from django.db import IntegrityError, connection, transaction

from eduflow.academics.models import AcademicYear, Section
from eduflow.attendance import services
from eduflow.attendance.models import AttendanceRecord, AttendanceSession
from eduflow.audit.models import AuditEvent
from eduflow.people.models import Enrollment, Student, TeacherAssignment
from eduflow.people.services import transfer_enrollment

from .conftest import TODAY, YESTERDAY, absent, actor, take

pytestmark = pytest.mark.django_db


def _statuses(session_id):
    return dict(
        AttendanceRecord.objects.filter(session_id=session_id).values_list("student__first_name", "status")
    )


# ------------------------------------------------------------------------------------------------ submitting
def test_a_teacher_marks_exceptions_and_everyone_else_is_present(world, class_a, school_day, api_as):
    teacher = api_as(world.teacher)
    summary = take(teacher, world.section_a, entries=absent(class_a.bala), client_id="draft-1")
    assert summary["class"] == {"id": str(world.section_a.pk), "label": "Grade 5 · A", "short_label": "5a"}
    assert (summary["date"], summary["total"], summary["locked"], summary["replayed"]) == (
        str(TODAY),
        3,
        False,
        False,
    )
    assert summary["counts"] == {"present": 2, "absent": 1, "late": 0, "half_day": 0, "excused": 0}
    assert _statuses(summary["session_id"]) == {"Asha": "present", "Bala": "absent", "Chitra": "present"}
    session = AttendanceSession.objects.get(pk=summary["session_id"])
    assert session.taken_by_id == world.teacher.pk
    assert (
        AuditEvent.objects.filter(action="attendance.register.submitted", target_id=session.pk).count() == 1
    )


def test_every_documented_status_is_accepted(world, class_a, school_day, api_as):
    entries = [
        {"student_id": str(class_a.asha.pk), "status": "late", "note": "Bus delay"},
        {"student_id": str(class_a.bala.pk), "status": "half_day"},
        {"student_id": str(class_a.chitra.pk), "status": "excused"},
    ]
    summary = take(api_as(world.teacher), world.section_a, entries=entries)
    assert summary["counts"] == {"present": 0, "absent": 0, "late": 1, "half_day": 1, "excused": 1}
    assert AttendanceRecord.objects.get(student=class_a.asha).note == "Bus delay"


def test_a_retry_with_the_same_client_id_changes_nothing(world, class_a, school_day, api_as):
    teacher = api_as(world.teacher)
    first = take(teacher, world.section_a, entries=absent(class_a.bala), client_id="draft-1")
    retry = take(teacher, world.section_a, entries=absent(class_a.chitra), client_id="draft-1")
    assert retry["replayed"] is True
    assert retry["session_id"] == first["session_id"]
    assert _statuses(first["session_id"])["Chitra"] == "present"  # the retried payload was not applied
    assert AuditEvent.objects.filter(action="attendance.register.submitted").count() == 1


def test_the_idempotency_key_header_also_works(world, class_a, school_day, api_as):
    teacher = api_as(world.teacher)
    take(teacher, world.section_a, entries=absent(class_a.bala), HTTP_IDEMPOTENCY_KEY="key-1")
    retry = take(teacher, world.section_a, entries=[], HTTP_IDEMPOTENCY_KEY="key-1")
    assert retry["replayed"] is True


def test_a_new_submission_replaces_the_register_until_it_locks(
    world, class_a, school_day, api_as, time_machine
):
    teacher = api_as(world.teacher)
    first = take(teacher, world.section_a, entries=absent(class_a.bala), client_id="draft-1")
    again = take(teacher, world.section_a, entries=absent(class_a.chitra, status="late"), client_id="draft-2")
    assert again["session_id"] == first["session_id"]
    assert _statuses(first["session_id"]) == {"Asha": "present", "Bala": "present", "Chitra": "late"}
    event = AuditEvent.objects.filter(action="attendance.register.submitted").latest("occurred_at")
    assert (event.metadata["replaced"], event.metadata["changed"]) == (True, 2)
    time_machine.move_to(datetime.datetime(2026, 7, 14, 18, 31, tzinfo=datetime.UTC))  # 00:01 on the 15th
    late = take(
        api_as(world.teacher), world.section_a, day=TODAY, entries=[], client_id="draft-3", expect=409
    )
    assert "correction" in late["error"]["message"]


def test_a_register_first_taken_for_an_earlier_date_is_locked_at_once(world, class_a, school_day, api_as):
    summary = take(api_as(world.teacher), world.section_a, day=YESTERDAY, entries=absent(class_a.asha))
    assert summary["locked"] is True
    take(api_as(world.teacher), world.section_a, day=YESTERDAY, entries=[], client_id="x", expect=409)


@pytest.mark.parametrize(
    ("day", "field"),
    [(TODAY + datetime.timedelta(days=1), "date"), (datetime.date(2026, 5, 31), "date")],
)
def test_future_dates_and_dates_outside_the_year_are_refused(world, class_a, school_day, api_as, day, field):
    response = take(api_as(world.teacher), world.section_a, day=day, expect=400)
    assert field in response["error"]["fields"]


def test_archived_sections_and_closed_years_are_refused(world, class_a, school_day, api_as):
    admin = api_as(world.admin)
    Section.objects.filter(pk=world.section_a.pk).update(status="archived")
    assert "section_id" in take(admin, world.section_a, expect=400)["error"]["fields"]
    Section.objects.filter(pk=world.section_a.pk).update(status="active")
    AcademicYear.objects.filter(pk=world.year.pk).update(status="closed", is_current=False)
    take(admin, world.section_a, expect=409)


def test_an_empty_class_has_no_register(world, school_day, api_as):
    empty = Section.objects.create(
        school=world.school, academic_year=world.year, grade=world.grade, name="Z", code="5z"
    )
    assert "date" in take(api_as(world.admin), empty, expect=400)["error"]["fields"]


@pytest.mark.parametrize(
    "entries",
    [
        "duplicate",
        "other_section",
        "other_school",
        "unknown",
    ],
)
def test_entries_must_name_students_on_the_roster_once(
    world, other_world, class_a, school_day, api_as, entries
):
    import uuid

    student_id = {
        "duplicate": class_a.bala.pk,
        "other_section": world.other_student.pk,
        "other_school": other_world.student.pk,
        "unknown": uuid.uuid4(),
    }[entries]
    body = [{"student_id": str(student_id), "status": "absent"}]
    if entries == "duplicate":
        body *= 2
    response = take(api_as(world.teacher), world.section_a, entries=body, expect=400)
    assert "entries" in response["error"]["fields"]
    assert not AttendanceSession.objects.exists()


def test_malformed_submissions_are_refused(world, class_a, school_day, api_as):
    teacher = api_as(world.teacher)
    bad_status = [{"student_id": str(class_a.bala.pk), "status": "holiday"}]
    assert "entries" in take(teacher, world.section_a, entries=bad_status, expect=400)["error"]["fields"]
    extra = take(teacher, world.section_a, entries=[], taken_by="me", expect=400)
    assert "taken_by" in extra["error"]["fields"]
    too_many = [{"student_id": str(class_a.bala.pk), "status": "absent"}] * 501
    assert "entries" in take(teacher, world.section_a, entries=too_many, expect=400)["error"]["fields"]


# ------------------------------------------------------------------------------------------------ who
def test_any_teacher_of_the_class_may_take_it_but_no_one_else(
    world, class_a, school_day, api_as, make_member
):
    from eduflow.academics.models import Subject
    from eduflow.people.models import StaffProfile

    science = make_member(world.school, roles=["teacher"])
    staff = StaffProfile.objects.create(school=world.school, membership=science, employee_id="T-9")
    subject = Subject.objects.create(school=world.school, name="Science", code="sci")
    TeacherAssignment.objects.create(
        school=world.school, staff=staff, academic_year=world.year, section=world.section_a, subject=subject
    )
    take(api_as(science), world.section_a)  # a subject teacher of the section
    take(api_as(world.teacher), world.section_b, expect=404)  # not their class
    api_as(world.teacher).get(f"classes/{world.section_b.pk}/roster", expect=404)
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(status="ended")
    take(api_as(world.teacher), world.section_a, expect=404)  # no longer teaches it
    for member in (world.parent, world.student_member):
        take(api_as(member), world.section_a, expect=403)
        api_as(member).get(f"classes/{world.section_a.pk}/roster", expect=403)
    for role in ("accountant", "hr_manager", "staff", "librarian"):
        take(api_as(make_member(world.school, roles=[role])), world.section_a, expect=403)


def test_the_school_takes_any_register(world, class_a, school_day, api_as):
    take(api_as(world.admin), world.section_b)
    take(api_as(world.principal), world.section_a)


def test_a_custom_section_scoped_role_still_needs_an_assignment(
    world, class_a, school_day, api_as, make_member
):
    from eduflow.authz.models import MembershipRole, Role
    from eduflow.authz.services import _set_grants, bump_rbac_version

    # A student given attendance.create over "self" sees their own section through the section rules, but
    # the service requires an active teaching assignment.
    role = Role.objects.create(school=world.school, key="monitor", name="Class monitor")
    _set_grants(role, {"attendance.create": ["self"]})
    MembershipRole.objects.create(school=world.school, membership=world.student_member, role=role)
    bump_rbac_version(world.school.pk)
    take(api_as(world.student_member), world.section_a, expect=403)
    api_as(world.student_member).get(f"classes/{world.section_a.pk}/roster", expect=403)


# ------------------------------------------------------------------------------------------------ roster
def test_the_roster_shows_the_class_and_its_marks(world, class_a, school_day, api_as):
    teacher = api_as(world.teacher)
    before = teacher.get(f"classes/{world.section_a.pk}/roster")
    assert (before["marked"], before["marked_at"], before["locked"]) == (False, None, False)
    assert [(s["name"], s["roll_no"], s["status"]) for s in before["students"]] == [
        ("Asha", "1", None),
        ("Bala", "2", None),
        ("Chitra", "3", None),
    ]
    assert before["students"][0]["initials"] == "A"
    assert before["cutoff"] == "2026-07-14T18:30:00Z"  # midnight in Asia/Kolkata, as a UTC timestamp
    take(teacher, world.section_a, entries=absent(class_a.chitra))
    after = teacher.get(f"classes/{world.section_a.pk}/roster?date={TODAY}")
    assert after["marked"] is True
    assert [s["status"] for s in after["students"]] == ["present", "present", "absent"]
    teacher.get(f"classes/{world.section_a.pk}/roster?date=2026-07-15", expect=400)  # future
    teacher.get(f"classes/{world.section_a.pk}/roster?when=today", expect=400)  # unknown parameter


def test_transfers_and_withdrawals_follow_the_enrollment_dates(world, class_a, school_day, api_as):
    enrollment = Enrollment.objects.select_related("academic_year").get(student=class_a.bala)
    transfer_enrollment(actor(world.admin), enrollment, section_id=world.section_b.pk, date=YESTERDAY)
    Enrollment.objects.filter(student=class_a.chitra).update(status="withdrawn", end_date=YESTERDAY)
    admin = api_as(world.admin)

    def names(section, day):
        roster = admin.get(f"classes/{section.pk}/roster?date={day}")["students"]
        return [s["name"] for s in roster]

    before = YESTERDAY - datetime.timedelta(days=1)
    assert names(world.section_a, before) == ["Asha", "Bala", "Chitra"]
    assert names(world.section_a, YESTERDAY) == ["Asha", "Chitra"]  # moved on the transfer day; last day
    assert names(world.section_b, YESTERDAY) == ["Bala", "Ravi"]
    assert names(world.section_a, TODAY) == ["Asha"]
    take(admin, world.section_a, entries=absent(class_a.bala), expect=400)  # no longer in this class


def test_a_retaken_register_drops_students_who_left(world, class_a, school_day, api_as):
    admin = api_as(world.admin)
    first = take(admin, world.section_a, client_id="a")
    Enrollment.objects.filter(student=class_a.chitra).update(status="withdrawn", end_date=YESTERDAY)
    again = take(admin, world.section_a, client_id="b")
    assert again["total"] == 2
    assert set(_statuses(first["session_id"])) == {"Asha", "Bala"}


# ------------------------------------------------------------------------------------------------ concurrency
@pytest.mark.django_db(transaction=True)
def test_parallel_submissions_leave_one_consistent_register(world, class_a, school_day):
    outcomes: list[str] = []
    barrier = threading.Barrier(3)

    def submit(index: int) -> None:
        try:
            barrier.wait()
            services.submit_register(
                actor(world.teacher),
                world.section_a,
                entries=[{"student_id": class_a.bala.pk, "status": "absent"}],
                client_id=f"c{index}",
            )
            outcomes.append("ok")
        except Exception as exc:  # each thread reports what stopped it
            outcomes.append(type(exc).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == ["ok", "ok", "ok"], outcomes  # serialised on the section row: later ones replace
    assert AttendanceSession.objects.count() == 1
    assert AttendanceRecord.objects.count() == 3


# ------------------------------------------------------------------------------------------------ database
def _session(world, day=TODAY):
    import django.utils.timezone as tz

    return AttendanceSession.objects.create(
        school=world.school,
        section=world.section_a,
        academic_year=world.year,
        date=day,
        taken_by=world.admin,
        submitted_at=tz.now(),
        locked_at=tz.now(),
    )


def test_database_refuses_a_second_register_and_records_for_strangers(world, other_world, class_a):
    session = _session(world)
    with pytest.raises(IntegrityError), transaction.atomic():
        _session(world)
    stranger = Student.objects.create(school=world.school, admission_number="X-1", first_name="X")
    other_enrollment = world.other_enrollment  # Ravi, section B
    values = {
        "school": world.school,
        "session": session,
        "status": "present",
        "section": world.section_a,
        "date": TODAY,
    }
    with pytest.raises(IntegrityError, match="attendance_record_enrollment_key_fk"), transaction.atomic():
        AttendanceRecord.objects.create(student=stranger, enrollment=other_enrollment, **values)
    with pytest.raises(IntegrityError, match="attendance_record_enrollment_key_fk"), transaction.atomic():
        AttendanceRecord.objects.create(student=world.other_student, enrollment=other_enrollment, **values)
    own = Enrollment.objects.get(student=class_a.bala)
    with pytest.raises(IntegrityError, match="attendance_record_session_key_fk"), transaction.atomic():
        AttendanceRecord.objects.create(student=class_a.bala, enrollment=own, **{**values, "date": YESTERDAY})
    with pytest.raises(IntegrityError, match="attendance_record_status_check"), transaction.atomic():
        AttendanceRecord.objects.create(
            student=class_a.bala, enrollment=own, **{**values, "status": "holiday"}
        )
    AttendanceRecord.objects.create(student=class_a.bala, enrollment=own, **values)
    with pytest.raises(IntegrityError, match="attendance_record_session_student_uniq"), transaction.atomic():
        AttendanceRecord.objects.create(student=class_a.bala, enrollment=own, **values)
