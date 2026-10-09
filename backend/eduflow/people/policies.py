"""Data-scope rules for people and for sections (ADR-020, docs/security/authorization.md#data-scopes).

All rules are relationships with the actor's **membership**, resolved in SQL (no per-request lookups):

=========  =================================================================================================
Scope      Meaning in Phase 3
=========  =================================================================================================
section    sections where the actor has an *active* teacher assignment (and what hangs off those sections)
child      students linked to the actor as a guardian (and their enrollments, sections and teachers)
self       the actor's own student / staff / guardian record, and for students their own sections
campus     records at the campus of the actor's staff profile
department records of the actor's staff profile's department
=========  =================================================================================================

Each multi-condition rule is one ``Q`` evaluated in a single ``filter()``, so its conditions apply to the
same related row (for example: *this* assignment is active **and** belongs to the actor).
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.academics.policies import sections
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource

from .models import Enrollment, Guardian, StaffProfile, Student, StudentGuardian, TeacherAssignment

ACTIVE = "active"

staff: ScopedResource[StaffProfile] = ScopedResource("staff", StaffProfile)
students: ScopedResource[Student] = ScopedResource("student", Student)
guardians: ScopedResource[Guardian] = ScopedResource("guardian", Guardian)
student_guardians: ScopedResource[StudentGuardian] = ScopedResource("student_guardian", StudentGuardian)
enrollments: ScopedResource[Enrollment] = ScopedResource("enrollment", Enrollment)
teacher_assignments: ScopedResource[TeacherAssignment] = ScopedResource(
    "teacher_assignment", TeacherAssignment
)


def _teaches(prefix: str, actor: Actor) -> Q:
    """``<prefix>teacher_assignments`` contains an active assignment of the actor."""
    return Q(
        **{
            f"{prefix}teacher_assignments__staff__membership": actor.membership,
            f"{prefix}teacher_assignments__status": ACTIVE,
        }
    )


def _guardian_of(prefix: str, actor: Actor) -> Q:
    return Q(**{f"{prefix}guardian_links__guardian__membership": actor.membership})


# ------------------------------------------------------------------------------------------------ sections
@sections.rule(DataScope.SECTION)
def _section_taught(actor: Actor) -> Q:
    return _teaches("", actor)


@sections.rule(DataScope.CHILD)
def _section_of_child(actor: Actor) -> Q:
    return Q(
        enrollments__status=ACTIVE,
        enrollments__student__guardian_links__guardian__membership=actor.membership,
    )


@sections.rule(DataScope.SELF)
def _section_of_self(actor: Actor) -> Q:
    return Q(enrollments__status=ACTIVE, enrollments__student__membership=actor.membership)


@sections.rule(DataScope.CAMPUS)
def _section_at_campus(actor: Actor) -> Q:
    return Q(campus__staff__membership=actor.membership)


# ------------------------------------------------------------------------------------------------ students
@students.rule(DataScope.SECTION)
def _student_taught(actor: Actor) -> Q:
    return Q(
        enrollments__status=ACTIVE,
        enrollments__section__teacher_assignments__staff__membership=actor.membership,
        enrollments__section__teacher_assignments__status=ACTIVE,
    )


@students.rule(DataScope.CHILD)
def _student_child(actor: Actor) -> Q:
    return _guardian_of("", actor)


@students.rule(DataScope.SELF)
def _student_self(actor: Actor) -> Q:
    return Q(membership=actor.membership)


@students.rule(DataScope.CAMPUS)
def _student_at_campus(actor: Actor) -> Q:
    return Q(enrollments__status=ACTIVE, enrollments__section__campus__staff__membership=actor.membership)


# ------------------------------------------------------------------------------------------------ enrollments
@enrollments.rule(DataScope.SECTION)
def _enrollment_taught(actor: Actor) -> Q:
    return _teaches("section__", actor)


@enrollments.rule(DataScope.CHILD)
def _enrollment_child(actor: Actor) -> Q:
    return _guardian_of("student__", actor)


@enrollments.rule(DataScope.SELF)
def _enrollment_self(actor: Actor) -> Q:
    return Q(student__membership=actor.membership)


# ------------------------------------------------------------------------------------------------ guardians
@guardians.rule(DataScope.SELF)
def _guardian_self(actor: Actor) -> Q:
    # Guardians see their own record; students see their own guardians.
    return Q(membership=actor.membership) | Q(student_links__student__membership=actor.membership)


@guardians.rule(DataScope.SECTION)
def _guardian_of_taught(actor: Actor) -> Q:
    return Q(
        student_links__student__enrollments__status=ACTIVE,
        student_links__student__enrollments__section__teacher_assignments__staff__membership=actor.membership,
        student_links__student__enrollments__section__teacher_assignments__status=ACTIVE,
    )


@student_guardians.rule(DataScope.SELF)
def _link_self(actor: Actor) -> Q:
    return Q(guardian__membership=actor.membership) | Q(student__membership=actor.membership)


@student_guardians.rule(DataScope.SECTION)
def _link_taught(actor: Actor) -> Q:
    return Q(
        student__enrollments__status=ACTIVE,
        student__enrollments__section__teacher_assignments__staff__membership=actor.membership,
        student__enrollments__section__teacher_assignments__status=ACTIVE,
    )


# ------------------------------------------------------------------------------------------------ staff
@staff.rule(DataScope.SELF)
def _staff_self(actor: Actor) -> Q:
    return Q(membership=actor.membership)


@staff.rule(DataScope.DEPARTMENT)
def _staff_same_department(actor: Actor) -> Q:
    return Q(department__staff__membership=actor.membership)


@staff.rule(DataScope.CAMPUS)
def _staff_same_campus(actor: Actor) -> Q:
    return Q(campus__staff__membership=actor.membership)


# ------------------------------------------------------------------------------------------------ assignments
@teacher_assignments.rule(DataScope.SELF)
def _assignment_self(actor: Actor) -> Q:
    # Teachers: their own assignments. Students: who teaches the sections they are enrolled in.
    return Q(staff__membership=actor.membership) | Q(
        section__enrollments__status=ACTIVE, section__enrollments__student__membership=actor.membership
    )


@teacher_assignments.rule(DataScope.SECTION)
def _assignment_co_teachers(actor: Actor) -> Q:
    return _teaches("section__", actor)


@teacher_assignments.rule(DataScope.CHILD)
def _assignment_child(actor: Actor) -> Q:
    return Q(
        section__enrollments__status=ACTIVE,
        section__enrollments__student__guardian_links__guardian__membership=actor.membership,
    )
