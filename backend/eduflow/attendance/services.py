"""Attendance writes: taking a register, and correcting it once locked (ADR-008, ADR-028).

**Taking a register** (``submit_register``) is a full replacement, as ADR-008 specifies: the client sends the
exceptions only, and every student on the roster who is not mentioned is ``present``. Re-sending it with the
same ``client_id`` replays the stored register without changing anything. Submissions for one section are
serialised by locking the section row; the unique ``(section, date)`` constraint is the backstop.

**Lock.** A register can be re-submitted until the end of its date in the school's time zone
(``locked_at``). After that, and for registers first taken for an earlier date, a change is an
``AttendanceCorrection``: requested with a reason, then approved or declined by someone else holding
``attendance.approve`` school-wide. Only an approved correction changes the record.

**Who.** Without a school-wide grant, only a teacher with an *active* assignment in the section may take its
register or request a correction (ADR-027: the scope loads the target, the service re-checks the
relationship). Closed academic years are read-only.
"""

from __future__ import annotations

import datetime
import zoneinfo
from collections.abc import Sequence
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import AcademicYearStatus, RecordStatus, Section
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.people.models import AssignmentStatus, TeacherAssignment
from eduflow.tenancy import domain

from . import selectors
from .models import (
    AttendanceCorrection,
    AttendanceRecord,
    AttendanceSession,
    AttendanceStatus,
    CorrectionStatus,
)


# --------------------------------------------------------------------------------------------- helpers
def _zone(actor: Actor) -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(actor.school.timezone)


def school_today(actor: Actor) -> datetime.date:
    return timezone.now().astimezone(_zone(actor)).date()


def lock_moment(actor: Actor, day: datetime.date) -> datetime.datetime:
    """The end of ``day`` in the school's time zone: when its register stops accepting re-submissions."""
    return datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(), tzinfo=_zone(actor))


def is_locked(session: AttendanceSession) -> bool:
    return timezone.now() >= session.locked_at


def _teaches(actor: Actor, section: Section, permission: str) -> None:
    """Without a school-wide grant, the caller must hold an active assignment in the section (ADR-027)."""
    if DataScope.SCHOOL in actor.scopes(permission):
        return
    if not TeacherAssignment.objects.filter(
        section=section, staff__membership=actor.membership, status=AssignmentStatus.ACTIVE
    ).exists():
        raise PermissionDenied("Only a teacher of this class can do that.")


def _open_year(section: Section) -> None:
    if section.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Attendance of a closed academic year is history and cannot be changed.")


def check_register_date(actor: Actor, section: Section, day: datetime.date) -> None:
    year = section.academic_year
    if not year.start_date <= day <= year.end_date:
        raise ValidationError({"date": ["The date is outside this section's academic year."]})
    if day > school_today(actor):
        raise ValidationError({"date": ["Attendance cannot be taken for a future date."]})


def can_take(actor: Actor, section: Section) -> None:
    """Reading a class roster to take its register needs the same authority as submitting it."""
    _teaches(actor, section, "attendance.create")


# --------------------------------------------------------------------------------------------- registers
def _validate_entries(
    entries: Sequence[dict[str, Any]], on_roster: dict[Any, Any]
) -> dict[Any, dict[str, Any]]:
    by_student: dict[Any, dict[str, Any]] = {}
    problems: list[str] = []
    for index, entry in enumerate(entries):
        student_id = entry["student_id"]
        if student_id in by_student:
            problems.append(f"Entry {index + 1}: this student appears more than once.")
        elif student_id not in on_roster:
            # The same answer for an unknown ID, another school's student and one not in this class that day.
            problems.append(f"Entry {index + 1}: this student is not on the class roster for that date.")
        by_student[student_id] = entry
    if problems:
        raise ValidationError({"entries": problems})
    return by_student


@transaction.atomic
def submit_register(
    actor: Actor,
    section: Section,
    *,
    date: datetime.date | None = None,
    entries: Sequence[dict[str, Any]] = (),
    client_id: str = "",
) -> tuple[AttendanceSession, bool]:
    """Take (or retake) a section's register. Returns ``(session, replayed)``."""
    section = (
        Section.objects.select_for_update(of=("self",)).select_related("academic_year").get(pk=section.pk)
    )
    _teaches(actor, section, "attendance.create")
    day = date or school_today(actor)
    existing = AttendanceSession.objects.filter(section=section, date=day).first()
    if existing is not None and client_id and existing.client_id == client_id:
        return existing, True  # a retry of an accepted submission: nothing changes (ADR-008)
    if section.status != RecordStatus.ACTIVE:
        raise ValidationError({"section_id": ["This section is archived."]})
    _open_year(section)
    check_register_date(actor, section, day)
    if existing is not None and is_locked(existing):
        raise Conflict("This register is locked. Request a correction for each change instead.")
    on_roster = {e.student_id: e for e in selectors.roster(section, day)}
    if not on_roster:
        raise ValidationError({"date": ["No students are enrolled in this section on that date."]})
    marks = _validate_entries(entries, on_roster)
    now = timezone.now()
    if existing is None:
        session = AttendanceSession(
            school=actor.school,
            section=section,
            academic_year_id=section.academic_year_id,
            date=day,
            taken_by=actor.membership,
            submitted_at=now,
            locked_at=lock_moment(actor, day),
            client_id=client_id,
        )
        session = domain.save(session, conflict="This register was taken at the same moment. Reload it.")
        previous: dict[Any, AttendanceRecord] = {}
    else:
        session = existing
        session.taken_by, session.submitted_at, session.client_id = actor.membership, now, client_id
        session.save(update_fields=["taken_by", "submitted_at", "client_id", "updated_at"])
        previous = {r.student_id: r for r in session.records.all()}
    changed = 0
    for student_id, enrollment in on_roster.items():
        entry = marks.get(student_id, {})
        status = entry.get("status", AttendanceStatus.PRESENT)
        note = entry.get("note", "")
        record = previous.pop(student_id, None)
        if record is None:
            AttendanceRecord.objects.create(
                school=actor.school,
                session=session,
                student_id=student_id,
                enrollment=enrollment,
                status=status,
                note=note,
                section_id=section.pk,
                date=day,
            )
        elif (record.status, record.note, record.enrollment_id) != (status, note, enrollment.pk):
            record.status, record.note, record.enrollment = status, note, enrollment
            record.save(update_fields=["status", "note", "enrollment", "updated_at"])
            changed += 1
    # Students who are no longer on the roster that day (enrollment ended or moved) leave the register.
    removed = [r.pk for r in previous.values()]
    if removed:
        AttendanceRecord.objects.filter(pk__in=removed).delete()
    tally = selectors.counts(list(session.records.values_list("status", flat=True)))
    domain.record(
        "attendance.register.submitted",
        session,
        section=str(section.pk),
        date=day.isoformat(),
        replaced=existing is not None or None,
        changed=changed or None,
        removed=len(removed) or None,
        **{k: v for k, v in tally.items() if v},
    )
    return session, False


# --------------------------------------------------------------------------------------------- corrections
_ONE_PENDING = "A correction of this record is already waiting for a decision."


@transaction.atomic
def request_correction(
    actor: Actor, record: AttendanceRecord, *, new_status: str, reason: str
) -> AttendanceCorrection:
    record = (
        AttendanceRecord.objects.select_for_update(of=("self",))
        .select_related("session", "section__academic_year")
        .get(pk=record.pk)
    )
    _teaches(actor, record.section, "attendance.update")
    _open_year(record.section)
    if not is_locked(record.session):
        raise Conflict("This register is still open: submit it again instead of requesting a correction.")
    if new_status == record.status:
        raise ValidationError({"new_status": ["The record already has this status."]})
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": ["Give a reason for the correction."]})
    try:
        with transaction.atomic():
            correction = AttendanceCorrection.objects.create(
                school=actor.school,
                record=record,
                old_status=record.status,
                new_status=new_status,
                reason=reason,
                requested_by=actor.membership,
            )
    except IntegrityError:
        raise Conflict(_ONE_PENDING) from None
    domain.record(
        "attendance.correction.requested",
        correction,
        record=str(record.pk),
        old_status=record.status,
        new_status=new_status,
    )
    return correction


def _pending(correction: AttendanceCorrection) -> AttendanceCorrection:
    locked = (
        AttendanceCorrection.objects.select_for_update(of=("self",))
        .select_related("record__section__academic_year")
        .get(pk=correction.pk)
    )
    if locked.status != CorrectionStatus.PENDING:
        raise Conflict(f"This correction is already {locked.status}.")
    _open_year(locked.record.section)
    return locked


def _decide(actor: Actor, correction: AttendanceCorrection, status: str, note: str) -> None:
    correction.status = status
    correction.decided_by = actor.membership
    correction.decided_at = timezone.now()
    correction.decision_note = note.strip()
    correction.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])


@transaction.atomic
def approve_correction(
    actor: Actor, correction: AttendanceCorrection, *, note: str = ""
) -> AttendanceCorrection:
    correction = _pending(correction)
    if correction.requested_by_id == actor.membership.pk:
        raise PermissionDenied("A correction must be approved by someone other than its requester.")
    record = AttendanceRecord.objects.select_for_update(of=("self",)).get(pk=correction.record_id)
    if record.status != correction.old_status:
        raise Conflict(
            "The record has changed since this correction was requested. Decline it and ask again."
        )
    record.status = correction.new_status
    record.save(update_fields=["status", "updated_at"])
    _decide(actor, correction, CorrectionStatus.APPROVED, note)
    domain.record("attendance.correction.approved", correction, record=str(record.pk))
    domain.record(
        "attendance.record.corrected",
        record,
        correction=str(correction.pk),
        old_status=correction.old_status,
        new_status=correction.new_status,
    )
    return correction


@transaction.atomic
def decline_correction(
    actor: Actor, correction: AttendanceCorrection, *, note: str = ""
) -> AttendanceCorrection:
    correction = _pending(correction)
    _decide(actor, correction, CorrectionStatus.DECLINED, note)
    domain.record("attendance.correction.declined", correction, record=str(correction.record_id))
    return correction
