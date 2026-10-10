"""Communication scopes.

Announcements (``self``): what is addressed to the caller. Staff see ``staff`` announcements (school-wide,
or for a section they teach); guardians ``parents`` announcements (school-wide, or for a section their child
is in); students ``students`` announcements (school-wide, or for their section). A teacher (``section``)
also sees every announcement of a section they teach. Archived announcements are never shown to recipients.

Threads (``self``): only their participants: the teacher, the guardian, or the student of a student-teacher
thread. Complaints: ``child`` (about the parent's child), ``self`` (raised by the caller).
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource
from eduflow.people.models import Guardian, StaffProfile, Student
from eduflow.people.scoping import children, own_sections, sections_of_children, taught_sections

from .models import Announcement, Audience, Complaint, Thread

announcements: ScopedResource[Announcement] = ScopedResource("announcement", Announcement)
threads: ScopedResource[Thread] = ScopedResource("thread", Thread)
complaints: ScopedResource[Complaint] = ScopedResource("complaint", Complaint)

LIVE = Q(archived_at__isnull=True)
SCHOOL_WIDE = Q(section__isnull=True)


def _for(audience: str) -> Q:
    return Q(audiences__contains=[audience])


@announcements.rule(DataScope.SELF)
def _addressed(actor: Actor) -> Q:
    rule = Q(pk__in=[])
    member = actor.membership
    if StaffProfile.objects.filter(membership=member).exists():
        rule |= _for(Audience.STAFF) & (SCHOOL_WIDE | taught_sections("section__", actor))
    if Guardian.objects.filter(membership=member).exists():
        rule |= _for(Audience.PARENTS) & (SCHOOL_WIDE | sections_of_children("section__", actor))
    if Student.objects.filter(membership=member).exists():
        rule |= _for(Audience.STUDENTS) & (SCHOOL_WIDE | own_sections("section__", actor))
    return rule & LIVE


@announcements.rule(DataScope.SECTION)
def _of_taught_sections(actor: Actor) -> Q:
    return taught_sections("section__", actor)


@threads.rule(DataScope.SELF)
def participant(actor: Actor) -> Q:
    return (
        Q(staff__membership=actor.membership)
        | Q(guardian__membership=actor.membership)
        | Q(kind="student_teacher", student__membership=actor.membership)
    )


complaints.rule(DataScope.CHILD)(lambda actor: children("student__", actor))
complaints.rule(DataScope.SELF)(lambda actor: Q(raised_by=actor.membership))
