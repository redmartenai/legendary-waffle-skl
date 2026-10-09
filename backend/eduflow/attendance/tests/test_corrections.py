"""Corrections of locked registers: request with a reason, approve or decline by someone else, history."""

import threading

import pytest
from django.db import IntegrityError, connection, transaction

from eduflow.academics.models import AcademicYear
from eduflow.attendance import services
from eduflow.attendance.models import AttendanceCorrection, AttendanceRecord
from eduflow.audit.models import AuditEvent
from eduflow.people.models import TeacherAssignment

from .conftest import TODAY, YESTERDAY, absent, actor, take

pytestmark = pytest.mark.django_db


@pytest.fixture
def locked(world, class_a, school_day, api_as):
    """Yesterday's register of section A (locked): Bala absent, the others present."""
    take(api_as(world.teacher), world.section_a, day=YESTERDAY, entries=absent(class_a.bala))
    return {r.student_id: r for r in AttendanceRecord.objects.filter(date=YESTERDAY)}


def _request(api, record, new_status="present", reason="Arrived after roll call", expect=201):
    body = {"record_id": str(record.pk), "new_status": new_status, "reason": reason}
    return api.post("attendance/corrections", body, expect=expect)


# ------------------------------------------------------------------------------------------------ requesting
def test_a_teacher_requests_a_correction_with_a_reason(world, class_a, locked, api_as):
    correction = _request(api_as(world.teacher), locked[class_a.bala.pk])
    assert (correction["status"], correction["old_status"], correction["new_status"]) == (
        "pending",
        "absent",
        "present",
    )
    assert correction["reason"] == "Arrived after roll call"
    assert correction["requested_by"]["full_name"] == world.teacher.user.full_name
    assert correction["decided_by"] is None
    assert AttendanceRecord.objects.get(pk=locked[class_a.bala.pk].pk).status == "absent"  # not yet applied
    assert AuditEvent.objects.filter(
        action="attendance.correction.requested", target_id=correction["id"]
    ).exists()


def test_request_rules(world, class_a, locked, api_as):
    teacher = api_as(world.teacher)
    record = locked[class_a.bala.pk]
    assert "new_status" in _request(teacher, record, new_status="absent", expect=400)["error"]["fields"]
    assert "reason" in _request(teacher, record, reason="   ", expect=400)["error"]["fields"]
    assert "reason" in _request(teacher, record, reason="", expect=400)["error"]["fields"]
    assert "new_status" in _request(teacher, record, new_status="holiday", expect=400)["error"]["fields"]
    _request(teacher, record)
    assert "already waiting" in _request(teacher, record, new_status="late", expect=409)["error"]["message"]


def test_an_open_register_is_resubmitted_not_corrected(world, class_a, school_day, api_as):
    take(api_as(world.teacher), world.section_a, entries=absent(class_a.bala))
    record = AttendanceRecord.objects.get(student=class_a.bala, date=TODAY)
    assert "submit it again" in _request(api_as(world.teacher), record, expect=409)["error"]["message"]


def test_only_teachers_of_the_class_and_the_school_request_corrections(
    world, class_a, locked, api_as, make_member
):
    record = locked[class_a.bala.pk]
    for member in (world.parent, world.student_member):
        _request(api_as(member), record, expect=403)
    other_teacher = make_member(world.school, roles=["teacher"])
    _request(api_as(other_teacher), record, expect=404)  # not in their scope
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(status="ended")
    _request(api_as(world.teacher), record, expect=404)
    _request(api_as(world.principal), record)


# ------------------------------------------------------------------------------------------------ deciding
def test_approval_applies_the_change_and_records_the_history(world, class_a, locked, api_as):
    requested = _request(api_as(world.teacher), locked[class_a.bala.pk])
    approved = api_as(world.principal).post(
        f"attendance/corrections/{requested['id']}/approve", {"note": "Gate log confirms"}
    )
    assert (approved["status"], approved["decision_note"]) == ("approved", "Gate log confirms")
    assert approved["decided_by"]["full_name"] == world.principal.user.full_name
    assert approved["decided_at"]
    record = AttendanceRecord.objects.get(pk=locked[class_a.bala.pk].pk)
    assert record.status == "present"
    corrected = AuditEvent.objects.get(action="attendance.record.corrected", target_id=record.pk)
    assert (corrected.metadata["old_status"], corrected.metadata["new_status"]) == ("absent", "present")
    month = api_as(world.admin).get(f"students/{class_a.bala.pk}/attendance?month=2026-07")
    assert month["summary"]["absent"] == 0
    assert next(d for d in month["days"] if d["date"] == str(YESTERDAY))["status"] == "present"
    history = api_as(world.admin).get(f"attendance/corrections?record_id={record.pk}")["results"]
    assert [h["status"] for h in history] == ["approved"]


def test_a_correction_can_be_corrected_again(world, class_a, locked, api_as):
    record = locked[class_a.bala.pk]
    first = _request(api_as(world.teacher), record)
    api_as(world.principal).post(f"attendance/corrections/{first['id']}/approve")
    second = _request(api_as(world.teacher), record, new_status="late", reason="Gate log says 09:20")
    assert second["old_status"] == "present"
    api_as(world.admin).post(f"attendance/corrections/{second['id']}/approve")
    history = api_as(world.admin).get(f"attendance/corrections?record_id={record.pk}")["results"]
    assert [(h["old_status"], h["new_status"]) for h in history] == [
        ("present", "late"),
        ("absent", "present"),
    ]


def test_decline_leaves_the_record_and_decisions_are_final(world, class_a, locked, api_as):
    requested = _request(api_as(world.teacher), locked[class_a.bala.pk])
    declined = api_as(world.admin).post(
        f"attendance/corrections/{requested['id']}/decline", {"note": "No proof"}
    )
    assert declined["status"] == "declined"
    assert AttendanceRecord.objects.get(pk=locked[class_a.bala.pk].pk).status == "absent"
    for action in ("approve", "decline"):
        api_as(world.principal).post(f"attendance/corrections/{requested['id']}/{action}", expect=409)
    _request(api_as(world.teacher), locked[class_a.bala.pk])  # a new request is possible again


def test_no_one_approves_their_own_request(world, class_a, locked, api_as):
    admin = api_as(world.admin)
    requested = _request(admin, locked[class_a.bala.pk])
    admin.post(f"attendance/corrections/{requested['id']}/approve", expect=403)
    assert AttendanceCorrection.objects.get(pk=requested["id"]).status == "pending"
    api_as(world.principal).post(f"attendance/corrections/{requested['id']}/approve")


def test_only_school_wide_approvers_decide(world, class_a, locked, api_as, make_member):
    requested = _request(api_as(world.teacher), locked[class_a.bala.pk])
    for member in (
        world.teacher,
        world.parent,
        world.student_member,
        make_member(world.school, roles=["hr_manager"]),
    ):
        api_as(member).post(f"attendance/corrections/{requested['id']}/approve", expect=403)
        api_as(member).post(f"attendance/corrections/{requested['id']}/decline", expect=403)
    assert AttendanceCorrection.objects.get(pk=requested["id"]).status == "pending"


def test_a_stale_correction_is_not_applied(world, class_a, locked, api_as):
    requested = _request(api_as(world.teacher), locked[class_a.bala.pk])
    AttendanceRecord.objects.filter(pk=locked[class_a.bala.pk].pk).update(status="late")
    api_as(world.principal).post(f"attendance/corrections/{requested['id']}/approve", expect=409)
    assert AttendanceRecord.objects.get(pk=locked[class_a.bala.pk].pk).status == "late"


def test_closed_years_are_history(world, class_a, locked, api_as):
    requested = _request(api_as(world.teacher), locked[class_a.bala.pk])
    AcademicYear.objects.filter(pk=world.year.pk).update(status="closed", is_current=False)
    api_as(world.principal).post(f"attendance/corrections/{requested['id']}/approve", expect=409)
    _request(api_as(world.admin), locked[class_a.chitra.pk], new_status="absent", expect=409)


# ------------------------------------------------------------------------------------------------ reading
def test_who_reads_corrections(world, class_a, locked, api_as, make_member):
    requested = _request(api_as(world.teacher), locked[class_a.bala.pk])

    def ids(member):
        return {c["id"] for c in api_as(member).get("attendance/corrections")["results"]}

    assert ids(world.teacher) == {requested["id"]}
    assert ids(world.principal) == {requested["id"]}
    assert ids(world.parent) == set()  # a whole-class workflow; parents read records instead
    assert ids(world.student_member) == set()
    assert ids(make_member(world.school, roles=["teacher"])) == set()
    pending = api_as(world.admin).get("attendance/corrections?status=pending")["results"]
    assert [c["id"] for c in pending] == [requested["id"]]
    api_as(world.parent).get(f"attendance/corrections/{requested['id']}", expect=404)


# ------------------------------------------------------------------------------------------------ integrity
@pytest.mark.django_db(transaction=True)
def test_parallel_requests_leave_one_pending(world, class_a, school_day, api_as):
    take(api_as(world.teacher), world.section_a, day=YESTERDAY, entries=absent(class_a.bala))
    record = AttendanceRecord.objects.get(student=class_a.bala, date=YESTERDAY)
    outcomes: list[str] = []
    barrier = threading.Barrier(3)

    def request() -> None:
        try:
            barrier.wait()
            services.request_correction(actor(world.teacher), record, new_status="present", reason="Late bus")
            outcomes.append("ok")
        except Exception as exc:  # each thread reports what stopped it
            outcomes.append(type(exc).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=request) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["Conflict", "Conflict", "ok"], outcomes
    assert AttendanceCorrection.objects.filter(status="pending").count() == 1


def test_database_keeps_corrections_consistent(world, class_a, locked):
    record = locked[class_a.bala.pk]
    base = {"school": world.school, "record": record, "reason": "x", "requested_by": world.teacher}
    with (
        pytest.raises(IntegrityError, match="attendance_correction_changes_status_check"),
        transaction.atomic(),
    ):
        AttendanceCorrection.objects.create(old_status="absent", new_status="absent", **base)
    with pytest.raises(IntegrityError, match="attendance_correction_decision_check"), transaction.atomic():
        AttendanceCorrection.objects.create(
            old_status="absent", new_status="present", status="approved", **base
        )
    with pytest.raises(IntegrityError, match="attendance_correction_reason_check"), transaction.atomic():
        AttendanceCorrection.objects.create(
            old_status="absent", new_status="present", **{**base, "reason": ""}
        )
    with pytest.raises(IntegrityError, match="attendance_correction_new_status_check"), transaction.atomic():
        AttendanceCorrection.objects.create(old_status="absent", new_status="holiday", **base)
