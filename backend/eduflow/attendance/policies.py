"""Data-scope rules for attendance (ADR-020, ADR-028).

=========  ===================================================================================================
Scope      What it covers
=========  ===================================================================================================
section    registers, records and corrections of sections where the actor has an *active* teacher assignment
child      records of students linked to the actor as a guardian
self       the actor's own records (an enrolled student)
campus     registers, records and corrections of sections at the campus of the actor's staff profile
=========  ===================================================================================================

A register (and a correction) concerns a whole class, so parents and students have no rule for them: they
read individual records and the per-student month view instead.

Writes with a narrow scope (a teacher taking the register of a class or requesting a correction) follow
ADR-027: the target is loaded through the caller's scope, and the service re-checks that the caller holds an
active assignment in that section, so a read-oriented rule can never authorise a write.
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource

from .models import AttendanceCorrection, AttendanceRecord, AttendanceSession

ACTIVE = "active"

sessions: ScopedResource[AttendanceSession] = ScopedResource("attendance_session", AttendanceSession)
records: ScopedResource[AttendanceRecord] = ScopedResource("attendance_record", AttendanceRecord)
corrections: ScopedResource[AttendanceCorrection] = ScopedResource(
    "attendance_correction", AttendanceCorrection
)


def _taught(actor: Actor, prefix: str) -> Q:
    return Q(
        **{
            f"{prefix}teacher_assignments__staff__membership": actor.membership,
            f"{prefix}teacher_assignments__status": ACTIVE,
        }
    )


def _at_campus(actor: Actor, prefix: str) -> Q:
    return Q(**{f"{prefix}campus__staff__membership": actor.membership})


# ------------------------------------------------------------------------------------------------ registers
@sessions.rule(DataScope.SECTION)
def _session_taught(actor: Actor) -> Q:
    return _taught(actor, "section__")


@sessions.rule(DataScope.CAMPUS)
def _session_campus(actor: Actor) -> Q:
    return _at_campus(actor, "section__")


# ------------------------------------------------------------------------------------------------ records
@records.rule(DataScope.SECTION)
def _record_taught(actor: Actor) -> Q:
    return _taught(actor, "section__")


@records.rule(DataScope.CHILD)
def _record_child(actor: Actor) -> Q:
    return Q(student__guardian_links__guardian__membership=actor.membership)


@records.rule(DataScope.SELF)
def _record_self(actor: Actor) -> Q:
    return Q(student__membership=actor.membership)


@records.rule(DataScope.CAMPUS)
def _record_campus(actor: Actor) -> Q:
    return _at_campus(actor, "section__")


# ------------------------------------------------------------------------------------------------ corrections
@corrections.rule(DataScope.SECTION)
def _correction_taught(actor: Actor) -> Q:
    return _taught(actor, "record__section__")


@corrections.rule(DataScope.CAMPUS)
def _correction_campus(actor: Actor) -> Q:
    return _at_campus(actor, "record__section__")
