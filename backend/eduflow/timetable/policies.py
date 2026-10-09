"""Data-scope rules for timetables, slots and lessons (ADR-020, docs/architecture/phase-5.md).

=========  ======================================================================================
Scope      Slots and lessons it covers
=========  ======================================================================================
section    sections where the actor has an *active* teacher assignment
self       the actor's own teaching (a teacher), or the actor's own section (an enrolled student)
child      sections where a child linked to the actor is actively enrolled
campus     sections at the campus of the actor's staff profile
=========  ======================================================================================

A timetable and its periods (name, dates, bell times) hold no personal data, so every scope that may read
timetables sees them. The rule is declared explicitly for each scope; an undeclared scope still grants
nothing.

``lesson.manage`` is the first write permission with a narrower scope than ``school``: a teacher records
lessons of their own slots. The services re-check that the actor is the slot's (or lesson's) teacher, so the
``self`` rule's student branch can never authorise a write.
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource

from .models import Lesson, Period, Timetable, TimetableSlot

ACTIVE = "active"

timetables: ScopedResource[Timetable] = ScopedResource("timetable", Timetable)
periods: ScopedResource[Period] = ScopedResource("timetable_period", Period)
slots: ScopedResource[TimetableSlot] = ScopedResource("timetable_slot", TimetableSlot)
lessons: ScopedResource[Lesson] = ScopedResource("lesson", Lesson)

_EVERY_ROW = Q(pk__isnull=False)  # not Q(): an empty Q would vanish when OR-ed with other rules


def _whole_plan(actor: Actor) -> Q:
    return _EVERY_ROW


for _scope in (DataScope.SECTION, DataScope.SELF, DataScope.CHILD, DataScope.CAMPUS):
    timetables.rule(_scope)(_whole_plan)
    periods.rule(_scope)(_whole_plan)


def _section_taught(actor: Actor, prefix: str) -> Q:
    return Q(
        **{
            f"{prefix}teacher_assignments__staff__membership": actor.membership,
            f"{prefix}teacher_assignments__status": ACTIVE,
        }
    )


def _section_of_student(actor: Actor, prefix: str) -> Q:
    return Q(
        **{
            f"{prefix}enrollments__status": ACTIVE,
            f"{prefix}enrollments__student__membership": actor.membership,
        }
    )


def _section_of_child(actor: Actor, prefix: str) -> Q:
    return Q(
        **{
            f"{prefix}enrollments__status": ACTIVE,
            f"{prefix}enrollments__student__guardian_links__guardian__membership": actor.membership,
        }
    )


# ------------------------------------------------------------------------------------------------ slots
@slots.rule(DataScope.SECTION)
def _slot_taught(actor: Actor) -> Q:
    return _section_taught(actor, "section__")


@slots.rule(DataScope.SELF)
def _slot_self(actor: Actor) -> Q:
    return Q(staff__membership=actor.membership) | _section_of_student(actor, "section__")


@slots.rule(DataScope.CHILD)
def _slot_child(actor: Actor) -> Q:
    return _section_of_child(actor, "section__")


@slots.rule(DataScope.CAMPUS)
def _slot_campus(actor: Actor) -> Q:
    return Q(section__campus__staff__membership=actor.membership)


# ------------------------------------------------------------------------------------------------ lessons
@lessons.rule(DataScope.SECTION)
def _lesson_taught(actor: Actor) -> Q:
    return _section_taught(actor, "section__")


@lessons.rule(DataScope.SELF)
def _lesson_self(actor: Actor) -> Q:
    return Q(staff__membership=actor.membership) | _section_of_student(actor, "section__")


@lessons.rule(DataScope.CHILD)
def _lesson_child(actor: Actor) -> Q:
    return _section_of_child(actor, "section__")


@lessons.rule(DataScope.CAMPUS)
def _lesson_campus(actor: Actor) -> Q:
    return Q(section__campus__staff__membership=actor.membership)
