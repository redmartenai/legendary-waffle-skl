"""People, enrollment and teaching writes. Transactional, school-checked and audited (``people.<entity>.*``).

Rules enforced here (with database constraints as the backstop, see people migration 0002):

* a profile's membership must be an **active membership of the same school**, and not already used by another
  profile of the same kind;
* an enrollment's academic year and grade are taken **from its section**, never from the client; the year must
  not be closed, the section and student must be active, capacity is respected, and a student has at most
  one active enrollment per year;
* enrollment lifecycle: ``active`` -> ``completed`` | ``withdrawn`` | ``transferred``; a transfer creates the
  new enrollment in the same academic year and links the two;
* a teacher assignment needs an active *teaching* staff profile, an active subject, and an open year.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import AcademicYearStatus, RecordStatus, Section, Subject
from eduflow.audit import services as audit
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.identity.phone import InvalidPhone, normalize_phone
from eduflow.tenancy import domain
from eduflow.tenancy.models import Membership

from .models import (
    AssignmentStatus,
    Enrollment,
    EnrollmentStatus,
    Guardian,
    StaffProfile,
    StaffStatus,
    StaffType,
    Student,
    StudentGuardian,
    StudentStatus,
    TeacherAssignment,
)


# --------------------------------------------------------------------------------------------- helpers
def _membership(actor: Actor, membership_id: Any, *, taken_by: str) -> Membership | None:
    """Resolve a membership to attach to a profile.

    Attaching a membership decides whose account *is* this student, guardian or staff member, which drives
    the ``self`` and ``child`` data scopes. So it needs school-wide member administration (``user.update``),
    and nobody can attach their own membership (that would let them grant themselves a child or self scope).
    """
    if membership_id is None:
        return None
    if DataScope.SCHOOL not in actor.scopes("user.update"):
        raise PermissionDenied("Linking an account to a profile needs school-wide member administration.")
    membership = domain.resolve(Membership, actor.school, membership_id, "membership_id", label="membership")
    if membership.pk == actor.membership.pk:
        raise ValidationError({"membership_id": ["You cannot link your own account to a profile."]})
    if not membership.is_active:
        raise ValidationError({"membership_id": ["This membership is not active."]})
    if hasattr(membership, taken_by):
        raise ValidationError({"membership_id": ["This member already has such a profile."]})
    return membership


def _ref(actor: Actor, data: dict[str, Any], key: str, model: type[Any], label: str) -> None:
    """Replace ``<name>_id`` in ``data`` by the object of the actor's school (or None)."""
    if key in data:
        pk = data.pop(key)
        data[key.removesuffix("_id")] = (
            domain.resolve(model, actor.school, pk, key, label=label) if pk else None
        )


def _normalise_phone(data: dict[str, Any]) -> None:
    if data.get("phone"):
        try:
            data["phone"] = normalize_phone(data["phone"])
        except InvalidPhone as exc:
            raise ValidationError({"phone": [str(exc)]}) from None


def _update(obj: Any, data: dict[str, Any], *, conflict: str, action: str) -> Any:
    changed = domain.apply_changes(obj, data)
    if changed:
        domain.save(obj, conflict=conflict, update_fields=changed)
        domain.record(action, obj, fields=changed)
    return obj


# --------------------------------------------------------------------------------------------- staff
_STAFF_CONFLICT = "That employee ID is already in use in this school."


def _staff_refs(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    from eduflow.academics.models import Campus, Department

    _ref(actor, data, "department_id", Department, "department")
    _ref(actor, data, "campus_id", Campus, "campus")
    for field in ("department", "campus"):
        ref = data.get(field)
        if ref is not None and ref.status != RecordStatus.ACTIVE:
            raise ValidationError({f"{field}_id": ["This record is archived."]})
    return data


@transaction.atomic
def create_staff(actor: Actor, *, membership_id: Any, **data: Any) -> StaffProfile:
    membership = _membership(actor, membership_id, taken_by="staff_profile")
    staff = domain.save(
        StaffProfile(school=actor.school, membership=membership, **_staff_refs(actor, data)),
        conflict=_STAFF_CONFLICT,
    )
    domain.record("people.staff.created", staff, membership=str(membership.pk) if membership else None)
    return staff


@transaction.atomic
def update_staff(actor: Actor, staff: StaffProfile, **data: Any) -> StaffProfile:
    updated: StaffProfile = _update(
        staff, _staff_refs(actor, data), conflict=_STAFF_CONFLICT, action="people.staff.updated"
    )
    if updated.status == StaffStatus.LEFT or updated.staff_type != StaffType.TEACHING:
        # Someone who left, or no longer teaches, keeps their history but loses their classes (and the
        # section scope that comes with them). On leave keeps them: the classes are still theirs.
        _end_assignments(
            TeacherAssignment.objects.for_school(actor.school).filter(staff=staff), "staff_changed"
        )
    return updated


def _end_assignments(qs: Any, reason: str) -> None:
    ended = [str(pk) for pk in qs.filter(status=AssignmentStatus.ACTIVE).values_list("pk", flat=True)]
    if ended:
        qs.filter(pk__in=ended).update(status=AssignmentStatus.ENDED)
        for pk in ended:
            audit.record(
                "people.teacher_assignment.ended",
                target_type="teacher_assignment",
                target_id=pk,
                metadata={"reason": reason},
            )


# --------------------------------------------------------------------------------------------- students
_STUDENT_CONFLICT = "That admission number is already in use in this school."


@transaction.atomic
def create_student(actor: Actor, *, membership_id: Any = None, **data: Any) -> Student:
    membership = _membership(actor, membership_id, taken_by="student_profile")
    student = domain.save(
        Student(school=actor.school, membership=membership, **data), conflict=_STUDENT_CONFLICT
    )
    domain.record("people.student.created", student)
    return student


@transaction.atomic
def update_student(actor: Actor, student: Student, **data: Any) -> Student:
    if "membership_id" in data:
        membership_id = data.pop("membership_id")
        if membership_id is None:
            data["membership"] = None
        elif student.membership_id != membership_id:
            data["membership"] = _membership(actor, membership_id, taken_by="student_profile")
    updated: Student = _update(student, data, conflict=_STUDENT_CONFLICT, action="people.student.updated")
    if updated.status != StudentStatus.ACTIVE:
        # A student who is no longer active leaves their section: teachers lose access, the seat frees up.
        final = (
            EnrollmentStatus.COMPLETED
            if updated.status == StudentStatus.GRADUATED
            else EnrollmentStatus.WITHDRAWN
        )
        for enrollment in Enrollment.objects.select_related("academic_year").filter(
            student=updated, status=EnrollmentStatus.ACTIVE
        ):
            _end(enrollment, final, None, f"Student status changed to {updated.status}.")
            domain.record(f"people.enrollment.{final}", enrollment, reason="student_status")
    return updated


# --------------------------------------------------------------------------------------------- guardians
@transaction.atomic
def create_guardian(actor: Actor, *, membership_id: Any = None, **data: Any) -> Guardian:
    membership = _membership(actor, membership_id, taken_by="guardian_profile")
    _normalise_phone(data)
    guardian = domain.save(
        Guardian(school=actor.school, membership=membership, **data), conflict="Duplicate."
    )
    domain.record("people.guardian.created", guardian)
    return guardian


@transaction.atomic
def update_guardian(actor: Actor, guardian: Guardian, **data: Any) -> Guardian:
    if "membership_id" in data:
        membership_id = data.pop("membership_id")
        if membership_id is None:
            data["membership"] = None
        elif guardian.membership_id != membership_id:
            data["membership"] = _membership(actor, membership_id, taken_by="guardian_profile")
    _normalise_phone(data)
    return _update(guardian, data, conflict="Duplicate.", action="people.guardian.updated")  # type: ignore[no-any-return]


def _clear_other_primary(link: StudentGuardian) -> None:
    StudentGuardian.objects.for_school(link.school_id).filter(
        student_id=link.student_id, is_primary=True
    ).exclude(pk=link.pk).update(is_primary=False)


@transaction.atomic
def link_guardian(actor: Actor, *, student_id: Any, guardian_id: Any, **data: Any) -> StudentGuardian:
    student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
    guardian = domain.resolve(Guardian, actor.school, guardian_id, "guardian_id", label="guardian")
    link = StudentGuardian(school=actor.school, student=student, guardian=guardian, **data)
    if link.is_primary:
        _clear_other_primary(link)
    domain.save(link, conflict="This guardian is already linked to this student.")
    domain.record(
        "people.guardian.linked",
        link,
        student=str(student.pk),
        guardian=str(guardian.pk),
        relationship=link.relationship,
    )
    return link


@transaction.atomic
def update_guardian_link(actor: Actor, link: StudentGuardian, **data: Any) -> StudentGuardian:
    if data.get("is_primary"):
        _clear_other_primary(link)
    return _update(link, data, conflict="Conflict.", action="people.guardian.link_updated")  # type: ignore[no-any-return]


@transaction.atomic
def unlink_guardian(actor: Actor, link: StudentGuardian) -> None:
    student_id, guardian_id = str(link.student_id), str(link.guardian_id)
    domain.delete(link)
    domain.record("people.guardian.unlinked", link, student=student_id, guardian=guardian_id)


# --------------------------------------------------------------------------------------------- enrollment
def _enrollable_section(actor: Actor, section_id: Any, field: str = "section_id") -> Section:
    section = domain.resolve(Section, actor.school, section_id, field, label="section")
    section = (
        Section.objects.select_related("academic_year").select_for_update(of=("self",)).get(pk=section.pk)
    )
    if section.academic_year.status == AcademicYearStatus.CLOSED:
        raise ValidationError({field: ["This section's academic year is closed."]})
    if section.status != RecordStatus.ACTIVE:
        raise ValidationError({field: ["This section is archived."]})
    if section.capacity is not None:
        enrolled = Enrollment.objects.filter(section=section, status=EnrollmentStatus.ACTIVE).count()
        if enrolled >= section.capacity:
            raise Conflict("This section is full.")
    return section


def _place(actor: Actor, student: Student, section: Section, **data: Any) -> Enrollment:
    enrollment = Enrollment(
        school=actor.school,
        student=student,
        section=section,
        academic_year_id=section.academic_year_id,
        grade_id=section.grade_id,
        **data,
    )
    return domain.save(
        enrollment,
        conflict=(
            "The student already has an active enrollment this academic year, or the roll number is taken."
        ),
    )


@transaction.atomic
def enroll(actor: Actor, *, student_id: Any, section_id: Any, **data: Any) -> Enrollment:
    student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
    if student.status != StudentStatus.ACTIVE:
        raise ValidationError({"student_id": ["Only active students can be enrolled."]})
    section = _enrollable_section(actor, section_id)
    year = section.academic_year
    data.setdefault("start_date", min(max(timezone.localdate(), year.start_date), year.end_date))
    _within_year(year, data["start_date"], "start_date")
    enrollment = _place(actor, student, section, **data)
    domain.record(
        "people.enrollment.created",
        enrollment,
        student=str(student.pk),
        section=str(section.pk),
        academic_year=str(section.academic_year_id),
    )
    return enrollment


def _within_year(year: Any, day: datetime.date, field: str) -> None:
    if not year.start_date <= day <= year.end_date:
        raise ValidationError(
            {field: [f"Must fall within the academic year ({year.start_date} to {year.end_date})."]}
        )


def _ensure_active(enrollment: Enrollment) -> None:
    if enrollment.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Enrollments of a closed academic year are history and cannot be changed.")
    if enrollment.status != EnrollmentStatus.ACTIVE:
        raise Conflict(f"This enrollment is already {enrollment.status}.")


def _end(
    enrollment: Enrollment, status: EnrollmentStatus, end_date: datetime.date | None, reason: str
) -> None:
    year = enrollment.academic_year
    end_date = end_date or min(max(timezone.localdate(), enrollment.start_date), year.end_date)
    if end_date < enrollment.start_date:
        raise ValidationError({"end_date": ["The end date cannot be before the start date."]})
    _within_year(year, end_date, "end_date")
    enrollment.status = status
    enrollment.end_date = end_date
    enrollment.status_reason = reason
    enrollment.save(update_fields=["status", "end_date", "status_reason", "updated_at"])


@transaction.atomic
def end_enrollment(
    actor: Actor,
    enrollment: Enrollment,
    *,
    status: str,
    end_date: datetime.date | None = None,
    reason: str = "",
) -> Enrollment:
    """Complete or withdraw an active enrollment."""
    if status not in (EnrollmentStatus.COMPLETED, EnrollmentStatus.WITHDRAWN):
        raise ValidationError({"status": ["Use completed or withdrawn; transfers have their own action."]})
    enrollment = (
        Enrollment.objects.select_for_update(of=("self",))
        .select_related("academic_year")
        .get(pk=enrollment.pk)
    )
    _ensure_active(enrollment)
    _end(enrollment, EnrollmentStatus(status), end_date, reason)
    domain.record(f"people.enrollment.{status}", enrollment, reason=reason or None)
    return enrollment


@transaction.atomic
def transfer_enrollment(
    actor: Actor,
    enrollment: Enrollment,
    *,
    section_id: Any,
    date: datetime.date | None = None,
    reason: str = "",
    roll_number: str = "",
) -> Enrollment:
    """Move an active enrollment to another section of the same academic year."""
    enrollment = (
        Enrollment.objects.select_for_update(of=("self",))
        .select_related("academic_year")
        .get(pk=enrollment.pk)
    )
    _ensure_active(enrollment)
    target = _enrollable_section(actor, section_id)
    if target.pk == enrollment.section_id:
        raise ValidationError({"section_id": ["The student is already in this section."]})
    if target.academic_year_id != enrollment.academic_year_id:
        raise ValidationError({"section_id": ["Transfers stay within the same academic year."]})
    date = date or min(max(timezone.localdate(), enrollment.start_date), enrollment.academic_year.end_date)
    _end(enrollment, EnrollmentStatus.TRANSFERRED, date, reason)
    new = _place(actor, enrollment.student, target, start_date=date, roll_number=roll_number)
    enrollment.transferred_to = new
    enrollment.save(update_fields=["transferred_to", "updated_at"])
    domain.record(
        "people.enrollment.transferred",
        enrollment,
        to_enrollment=str(new.pk),
        from_section=str(enrollment.section_id),
        to_section=str(target.pk),
        reason=reason or None,
    )
    return new


@transaction.atomic
def update_enrollment(actor: Actor, enrollment: Enrollment, **data: Any) -> Enrollment:
    _ensure_active(enrollment)
    return _update(  # type: ignore[no-any-return]
        enrollment,
        data,
        conflict="That roll number is already taken in this section.",
        action="people.enrollment.updated",
    )


# --------------------------------------------------------------------------------------------- teaching
_ASSIGNMENT_CONFLICT = "This assignment already exists, or the section already has an active class teacher."


@transaction.atomic
def assign_teacher(
    actor: Actor, *, staff_id: Any, section_id: Any, subject_id: Any = None, is_class_teacher: bool = False
) -> TeacherAssignment:
    staff = domain.resolve(StaffProfile, actor.school, staff_id, "staff_id", label="staff member")
    if staff.staff_type != StaffType.TEACHING or staff.status != StaffStatus.ACTIVE:
        raise ValidationError({"staff_id": ["Only active teaching staff can be assigned."]})
    section = domain.resolve(Section, actor.school, section_id, "section_id", label="section")
    if section.academic_year.status == AcademicYearStatus.CLOSED:
        raise ValidationError({"section_id": ["This section's academic year is closed."]})
    if section.status != RecordStatus.ACTIVE:
        raise ValidationError({"section_id": ["This section is archived."]})
    subject = None
    if subject_id is not None:
        subject = domain.resolve(Subject, actor.school, subject_id, "subject_id", label="subject")
        if subject.status != RecordStatus.ACTIVE:
            raise ValidationError({"subject_id": ["This subject is archived."]})
    elif not is_class_teacher:
        raise ValidationError({"subject_id": ["Give a subject, or make this a class-teacher assignment."]})
    assignment = domain.save(
        TeacherAssignment(
            school=actor.school,
            staff=staff,
            section=section,
            academic_year_id=section.academic_year_id,
            subject=subject,
            is_class_teacher=is_class_teacher,
        ),
        conflict=_ASSIGNMENT_CONFLICT,
    )
    domain.record(
        "people.teacher_assignment.created",
        assignment,
        staff=str(staff.pk),
        section=str(section.pk),
        subject=str(subject.pk) if subject else None,
        is_class_teacher=is_class_teacher or None,
    )
    return assignment


def _ensure_assignment_editable(assignment: TeacherAssignment) -> None:
    if assignment.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Assignments of a closed academic year are history and cannot be changed.")
    if assignment.status == AssignmentStatus.ENDED:
        raise Conflict("An ended assignment is history and cannot be changed.")


@transaction.atomic
def update_assignment(actor: Actor, assignment: TeacherAssignment, **data: Any) -> TeacherAssignment:
    _ensure_assignment_editable(assignment)
    if data.get("is_class_teacher") is False and assignment.subject_id is None:
        raise ValidationError(
            {"is_class_teacher": ["A homeroom-only assignment must stay a class-teacher one."]}
        )
    return _update(  # type: ignore[no-any-return]
        assignment, data, conflict=_ASSIGNMENT_CONFLICT, action="people.teacher_assignment.updated"
    )


@transaction.atomic
def delete_assignment(actor: Actor, assignment: TeacherAssignment) -> None:
    """For mistakes only: an ended assignment, or one in a closed year, is teaching history and stays."""
    _ensure_assignment_editable(assignment)
    domain.delete(assignment)
    domain.record("people.teacher_assignment.deleted", assignment)


# --------------------------------------------------------------------------------------------- year closing
def wind_down_year(sender: Any, *, actor: Actor, year: Any, **_: Any) -> None:
    """Receiver of ``academics.services.year_closing``: a closed year leaves nothing active behind.

    Active enrollments are completed (on the year's last day, or today if earlier) and active teacher
    assignments end, so nobody keeps access to students through a finished year. Each change is audited.
    """
    for enrollment in Enrollment.objects.select_related("academic_year").filter(
        academic_year=year, status=EnrollmentStatus.ACTIVE
    ):
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.end_date = max(enrollment.start_date, min(timezone.localdate(), year.end_date))
        enrollment.status_reason = "Academic year closed."
        enrollment.save(update_fields=["status", "end_date", "status_reason", "updated_at"])
        domain.record("people.enrollment.completed", enrollment, reason="year_closed")
    _end_assignments(TeacherAssignment.objects.filter(academic_year=year), "year_closed")
