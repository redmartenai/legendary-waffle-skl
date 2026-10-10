"""Who sees which document: the caller's data scope **and** the document's audience list.

========  =============================================================================================
Scope     Documents covered
========  =============================================================================================
school    every document of the school
section   about a student the teacher teaches (audience ``teachers``, or ``class_teacher`` for that
          section's class teacher); school-wide documents for ``teachers`` or ``staff``
child     about the parent's child, audience ``parents``; school-wide documents for ``parents``
self      about the caller as a student (``students``) or staff member (``staff``); school-wide documents
          for ``students`` (a student caller) or ``staff`` (a staff caller)
========  =============================================================================================
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource
from eduflow.people.models import StaffProfile, Student

from .models import Audience, Document

documents: ScopedResource[Document] = ScopedResource("document", Document)

ACTIVE = "active"
SCHOOL_WIDE = Q(student__isnull=True, staff__isnull=True)


def _has(audience: str) -> Q:
    return Q(audiences__contains=[audience])


@documents.rule(DataScope.SECTION)
def _taught(actor: Actor) -> Q:
    taught = Q(
        student__enrollments__status=ACTIVE,
        student__enrollments__section__teacher_assignments__staff__membership=actor.membership,
        student__enrollments__section__teacher_assignments__status=ACTIVE,
    )
    class_teacher = Q(
        student__enrollments__status=ACTIVE,
        student__enrollments__section__teacher_assignments__staff__membership=actor.membership,
        student__enrollments__section__teacher_assignments__status=ACTIVE,
        student__enrollments__section__teacher_assignments__is_class_teacher=True,
    )
    return (
        (taught & _has(Audience.TEACHERS))
        | (class_teacher & _has(Audience.CLASS_TEACHER))
        | (SCHOOL_WIDE & (_has(Audience.TEACHERS) | _has(Audience.STAFF)))
    )


@documents.rule(DataScope.CHILD)
def _child(actor: Actor) -> Q:
    return (Q(student__guardian_links__guardian__membership=actor.membership) & _has(Audience.PARENTS)) | (
        SCHOOL_WIDE & _has(Audience.PARENTS)
    )


@documents.rule(DataScope.SELF)
def _self(actor: Actor) -> Q:
    rule = (Q(student__membership=actor.membership) & _has(Audience.STUDENTS)) | (
        Q(staff__membership=actor.membership) & _has(Audience.STAFF)
    )
    if Student.objects.filter(membership=actor.membership).exists():
        rule |= SCHOOL_WIDE & _has(Audience.STUDENTS)
    if StaffProfile.objects.filter(membership=actor.membership).exists():
        rule |= SCHOOL_WIDE & _has(Audience.STAFF)
    return rule
