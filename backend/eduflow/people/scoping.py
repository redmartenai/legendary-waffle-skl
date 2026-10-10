"""Reusable data-scope rules for records that hang off a student, a section or a staff member.

Later modules (homework, conduct, assessment, fees, library, transport, hostel...) own records *about* a
student or *for* a section. Their scopes mean the same thing everywhere, so the rules are written once here
and registered on each module's :class:`ScopedResource`:

=========  ===================================================================================================
Scope      A record is covered when...
=========  ===================================================================================================
section    its student is actively enrolled in, or its section is, a section the actor actively teaches
child      its student is linked to the actor as a guardian (or its section holds such a student)
self       its student is the actor (or its section holds the actor), or its staff member is the actor
=========  ===================================================================================================

``prefix`` is the lookup path from the record to the student / section / staff profile, ending in ``__``
(``"student__"``, ``"submission__student__"``...). Rules are single ``Q`` objects so their conditions apply
to the same related row.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource

ACTIVE = "active"


def taught_students(prefix: str, actor: Actor) -> Q:
    return Q(
        **{
            f"{prefix}enrollments__status": ACTIVE,
            f"{prefix}enrollments__section__teacher_assignments__staff__membership": actor.membership,
            f"{prefix}enrollments__section__teacher_assignments__status": ACTIVE,
        }
    )


def children(prefix: str, actor: Actor) -> Q:
    return Q(**{f"{prefix}guardian_links__guardian__membership": actor.membership})


def own_student(prefix: str, actor: Actor) -> Q:
    return Q(**{f"{prefix}membership": actor.membership})


def taught_sections(prefix: str, actor: Actor) -> Q:
    return Q(
        **{
            f"{prefix}teacher_assignments__staff__membership": actor.membership,
            f"{prefix}teacher_assignments__status": ACTIVE,
        }
    )


def sections_of_children(prefix: str, actor: Actor) -> Q:
    return Q(
        **{
            f"{prefix}enrollments__status": ACTIVE,
            f"{prefix}enrollments__student__guardian_links__guardian__membership": actor.membership,
        }
    )


def own_sections(prefix: str, actor: Actor) -> Q:
    return Q(
        **{
            f"{prefix}enrollments__status": ACTIVE,
            f"{prefix}enrollments__student__membership": actor.membership,
        }
    )


def student_rules(resource: ScopedResource[Any], prefix: str = "student__") -> None:
    """SECTION / CHILD / SELF over a record that belongs to one student."""
    resource.rule(DataScope.SECTION)(lambda actor: taught_students(prefix, actor))
    resource.rule(DataScope.CHILD)(lambda actor: children(prefix, actor))
    resource.rule(DataScope.SELF)(lambda actor: own_student(prefix, actor))


def section_rules(resource: ScopedResource[Any], prefix: str = "section__") -> None:
    """SECTION / CHILD / SELF over a record that belongs to one section (homework, exams, announcements)."""
    resource.rule(DataScope.SECTION)(lambda actor: taught_sections(prefix, actor))
    resource.rule(DataScope.CHILD)(lambda actor: sections_of_children(prefix, actor))
    resource.rule(DataScope.SELF)(lambda actor: own_sections(prefix, actor))


def staff_self_rule(resource: ScopedResource[Any], prefix: str = "staff__") -> None:
    """SELF over a record that belongs to one staff member (leave, payslips, staff attendance)."""
    resource.rule(DataScope.SELF)(lambda actor: Q(**{f"{prefix}membership": actor.membership}))


def teaches(actor: Actor, section: Any, subject: Any = None) -> bool:
    """The actor has an active assignment in the section (for that subject, when one is given; a class
    teacher's assignment covers every subject of the section)."""
    from .models import TeacherAssignment

    rows = TeacherAssignment.objects.filter(
        school_id=actor.school.pk, staff__membership=actor.membership, section=section, status=ACTIVE
    )
    if subject is not None:
        rows = rows.filter(Q(subject=subject) | Q(is_class_teacher=True))
    return rows.exists()


def may_write(actor: Actor, permission: str, section: Any, subject: Any = None) -> bool:
    """Narrow writes (ADR-027): school-wide grant, or a section grant plus an active assignment there."""
    scopes = actor.scopes(permission)
    if DataScope.SCHOOL in scopes:
        return True
    return DataScope.SECTION in scopes and teaches(actor, section, subject)


def may_write_for_student(actor: Actor, permission: str, student: Any) -> bool:
    """Narrow writes about one student: school-wide grant, or a section grant and the actor teaches them."""
    from .models import Student

    scopes = actor.scopes(permission)
    if DataScope.SCHOOL in scopes:
        return True
    return (
        DataScope.SECTION in scopes
        and Student.objects.filter(pk=student.pk).filter(taught_students("", actor)).exists()
    )
