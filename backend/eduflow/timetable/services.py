"""Timetable writes: building, publishing and archiving timetables, and recording lessons (ADR-026).

Every write runs in a transaction, resolves each client-supplied ID inside the actor's school, and is audited
(``timetable.<entity>.<action>``). Writes to a timetable, its periods and its slots lock the timetable row
first, so edits of one timetable are serialised; clashes *between* timetables are caught by the database's
exclusion constraints even when two timetables are edited at the same time.

Clash checks run twice: here, to give a precise message ("Ms Rao already teaches 5-B on Monday 09:00-09:45
in Term 1"), and in the database, which is the guarantee. A constraint violation that slips past the
pre-check (a concurrent edit) still becomes a ``409``.
"""

from __future__ import annotations

import dataclasses
import datetime
import zoneinfo
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import (
    AcademicYear,
    AcademicYearStatus,
    RecordStatus,
    Room,
    Section,
    Term,
)
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.people.models import AssignmentStatus, TeacherAssignment
from eduflow.tenancy import domain

from .models import (
    Lesson,
    LessonStatus,
    Period,
    SlotKind,
    Timetable,
    TimetableSlot,
    TimetableStatus,
)

WEEKDAYS = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}
_CLASH = "This clashes with another slot (same section, teacher or room at the same time)."
_NAME_OR_CLASH = "The name is taken, or the new dates clash with another published timetable."
_MAX_REPORTED = 3


# --------------------------------------------------------------------------------------------- helpers
def _ensure_editable(timetable: Timetable) -> None:
    if timetable.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Timetables of a closed academic year are history and cannot be changed.")
    if timetable.status == TimetableStatus.ARCHIVED:
        raise Conflict("An archived timetable is history and cannot be changed. Copy it instead.")


def _lock(timetable_id: Any) -> Timetable:
    return (
        Timetable.objects.select_for_update(of=("self",)).select_related("academic_year").get(pk=timetable_id)
    )


def _lock_editable(timetable_id: Any) -> Timetable:
    timetable = _lock(timetable_id)
    _ensure_editable(timetable)
    return timetable


def _check_dates(year: AcademicYear, term: Term | None, start: datetime.date, end: datetime.date) -> None:
    if start > end:
        raise ValidationError({"effective_to": ["The end date must not be before the start date."]})
    if start < year.start_date or end > year.end_date:
        raise ValidationError({"effective_from": ["A timetable must lie within its academic year."]})
    if term is not None and (start < term.start_date or end > term.end_date):
        raise ValidationError({"effective_from": ["A timetable tied to a term must lie within the term."]})


def school_today(actor: Actor) -> datetime.date:
    return timezone.now().astimezone(zoneinfo.ZoneInfo(actor.school.timezone)).date()


# --------------------------------------------------------------------------------------------- clash checks
@dataclass(frozen=True)
class _Spec:
    """What a slot occupies: the inputs of the exclusion constraints."""

    slot_id: Any
    weekday: int
    start: datetime.time
    end: datetime.time
    effective_from: datetime.date
    effective_to: datetime.date
    section_id: Any
    staff_id: Any
    room_id: Any

    def places(self) -> Iterable[tuple[str, Any, int]]:
        yield ("section", self.section_id, self.weekday)
        if self.staff_id is not None:
            yield ("staff", self.staff_id, self.weekday)
        if self.room_id is not None:
            yield ("room", self.room_id, self.weekday)

    def overlaps(self, other: _Spec) -> bool:
        return (
            self.start < other.end
            and other.start < self.end
            and self.effective_from <= other.effective_to
            and other.effective_from <= self.effective_to
        )


def _spec(slot: TimetableSlot, **override: Any) -> _Spec:
    spec = _Spec(
        slot_id=slot.pk,
        weekday=slot.weekday,
        start=slot.start_time,
        end=slot.end_time,
        effective_from=slot.effective_from,
        effective_to=slot.effective_to,
        section_id=slot.section_id,
        staff_id=slot.staff_id,
        room_id=slot.room_id,
    )
    return dataclasses.replace(spec, **override) if override else spec


def _describe(kind: str, other: TimetableSlot) -> str:
    who = {
        "section": f"Section {other.section.code}",
        "staff": other.staff.membership.user.full_name if other.staff else "",
        "room": f"Room {other.room.code}" if other.room else "",
    }[kind]
    what = f"{other.subject.name} with {other.section.code}" if other.subject else other.title
    return (
        f"{who} already has {what} on {WEEKDAYS[other.weekday]} "
        f"{other.start_time:%H:%M}-{other.end_time:%H:%M} ({other.timetable.name})"
    )


def _raise_clashes(mine: list[_Spec], others: QuerySet[TimetableSlot]) -> None:
    """Compare candidate slots with existing ones and raise ``409`` naming the first few clashes."""
    if not mine:
        return
    index: dict[tuple[str, Any, int], list[tuple[_Spec, TimetableSlot]]] = defaultdict(list)
    for other in others.select_related("section", "subject", "room", "timetable", "staff__membership__user"):
        spec = _spec(other)
        for key in spec.places():
            index[key].append((spec, other))
    found: list[str] = []
    mine_ids = {m.slot_id for m in mine}
    for spec in mine:
        for key in spec.places():
            for other_spec, other in index.get(key, ()):
                if other_spec.slot_id not in mine_ids and spec.overlaps(other_spec):
                    found.append(_describe(key[0], other))
    if found:
        unique = list(dict.fromkeys(found))
        more = f" (and {len(unique) - _MAX_REPORTED} more)" if len(unique) > _MAX_REPORTED else ""
        raise Conflict("Clash: " + "; ".join(unique[:_MAX_REPORTED]) + more + ".")


def _live_others(school_id: Any, *, exclude_timetable: Any, start: datetime.date, end: datetime.date) -> Any:
    """Live slots of *other* timetables whose dates overlap ``[start, end]``."""
    return (
        TimetableSlot.objects.for_school(school_id)
        .filter(is_live=True, effective_from__lte=end, effective_to__gte=start)
        .exclude(timetable_id=exclude_timetable)
    )


def _check_slot(slot: TimetableSlot) -> None:
    """Pre-check one new or changed slot: within its timetable, and against every live timetable."""
    same_place = Q(section_id=slot.section_id)
    if slot.staff_id is not None:
        same_place |= Q(staff_id=slot.staff_id)
    if slot.room_id is not None:
        same_place |= Q(room_id=slot.room_id)
    spec = _spec(slot)
    own = TimetableSlot.objects.filter(timetable_id=slot.timetable_id, weekday=slot.weekday).filter(
        same_place
    )
    _raise_clashes([spec], own.exclude(pk=slot.pk))
    if slot.is_live:
        others = _live_others(
            slot.school_id,
            exclude_timetable=slot.timetable_id,
            start=slot.effective_from,
            end=slot.effective_to,
        ).filter(same_place, weekday=slot.weekday)
        _raise_clashes([spec], others)


def _check_timetable_against_live(
    timetable: Timetable,
    start: datetime.date,
    end: datetime.date,
    *,
    periods: dict[Any, Period] | None = None,
) -> None:
    """Would ``timetable``'s slots, live between ``start`` and ``end``, clash with other live timetables?"""
    periods = periods or {}
    mine = []
    for slot in timetable.slots.all():
        period = periods.get(slot.period_id)
        times = {"start": period.start_time, "end": period.end_time} if period else {}
        mine.append(_spec(slot, effective_from=start, effective_to=end, **times))
    _raise_clashes(
        mine, _live_others(timetable.school_id, exclude_timetable=timetable.pk, start=start, end=end)
    )


# --------------------------------------------------------------------------------------------- timetables
_NAME_TAKEN = "A timetable with that name already exists in this academic year."


@transaction.atomic
def create_timetable(
    actor: Actor,
    *,
    academic_year_id: Any,
    name: str,
    term_id: Any = None,
    effective_from: datetime.date | None = None,
    effective_to: datetime.date | None = None,
) -> Timetable:
    year = domain.resolve(
        AcademicYear, actor.school, academic_year_id, "academic_year_id", label="academic year"
    )
    if year.status == AcademicYearStatus.CLOSED:
        raise ValidationError({"academic_year_id": ["This academic year is closed."]})
    term = None
    if term_id is not None:
        term = domain.resolve(Term, actor.school, term_id, "term_id", label="term")
        if term.academic_year_id != year.pk:
            raise ValidationError({"term_id": ["This term belongs to another academic year."]})
    default_from, default_to = (term.start_date, term.end_date) if term else (year.start_date, year.end_date)
    start, end = effective_from or default_from, effective_to or default_to
    _check_dates(year, term, start, end)
    timetable = domain.save(
        Timetable(
            school=actor.school,
            academic_year=year,
            term=term,
            name=name,
            effective_from=start,
            effective_to=end,
        ),
        conflict=_NAME_TAKEN,
    )
    domain.record("timetable.timetable.created", timetable, academic_year=str(year.pk))
    return timetable


@transaction.atomic
def update_timetable(actor: Actor, timetable: Timetable, **data: Any) -> Timetable:
    timetable = _lock_editable(timetable.pk)
    if "term_id" in data:
        term_id = data.pop("term_id")
        if timetable.status != TimetableStatus.DRAFT:
            raise ValidationError({"term_id": ["The term of a published timetable cannot change."]})
        term = domain.resolve(Term, actor.school, term_id, "term_id", label="term") if term_id else None
        if term is not None and term.academic_year_id != timetable.academic_year_id:
            raise ValidationError({"term_id": ["This term belongs to another academic year."]})
        data["term"] = term
    start = data.get("effective_from", timetable.effective_from)
    end = data.get("effective_to", timetable.effective_to)
    _check_dates(timetable.academic_year, data.get("term", timetable.term), start, end)
    if timetable.is_live and (start, end) != (timetable.effective_from, timetable.effective_to):
        _check_timetable_against_live(timetable, start, end)
    changed = domain.apply_changes(timetable, data)
    if changed:
        # New dates cascade to the slots (timetable migration 0002) and re-run the clash constraints.
        domain.save(timetable, conflict=_NAME_OR_CLASH, update_fields=changed)
        domain.record("timetable.timetable.updated", timetable, fields=changed)
    return timetable


@transaction.atomic
def delete_timetable(actor: Actor, timetable: Timetable) -> None:
    timetable = _lock(timetable.pk)
    if timetable.status != TimetableStatus.DRAFT:
        raise Conflict("Only a draft timetable can be deleted. Archive a published one instead.")
    domain.delete(timetable)
    domain.record("timetable.timetable.deleted", timetable)


@transaction.atomic
def publish_timetable(actor: Actor, timetable: Timetable) -> Timetable:
    timetable = _lock_editable(timetable.pk)
    if timetable.status != TimetableStatus.DRAFT:
        raise Conflict(f"This timetable is {timetable.status}; only a draft can be published.")
    slots = timetable.slots.all()
    if not slots.exists():
        raise ValidationError({"non_field_errors": ["Add at least one slot before publishing."]})
    stale = slots.filter(kind=SlotKind.LESSON).exclude(assignment__status=AssignmentStatus.ACTIVE).count()
    if stale:
        raise ValidationError(
            {
                "non_field_errors": [
                    f"{stale} slot(s) use a teacher assignment that has ended. Reassign them first."
                ]
            }
        )
    _check_timetable_against_live(timetable, timetable.effective_from, timetable.effective_to)
    timetable.status = TimetableStatus.PUBLISHED
    timetable.is_live = True
    timetable.published_at = timezone.now()
    # is_live cascades to every slot, which brings them under the cross-timetable clash constraints.
    domain.save(timetable, conflict=_CLASH, update_fields=["status", "is_live", "published_at"])
    domain.record("timetable.timetable.published", timetable, slots=slots.count())
    return timetable


@transaction.atomic
def archive_timetable(actor: Actor, timetable: Timetable) -> Timetable:
    timetable = _lock(timetable.pk)
    if timetable.status != TimetableStatus.PUBLISHED:
        raise Conflict(f"This timetable is {timetable.status}; only a published one can be archived.")
    timetable.status = TimetableStatus.ARCHIVED
    timetable.is_live = False
    timetable.archived_at = timezone.now()
    domain.save(timetable, conflict=_CLASH, update_fields=["status", "is_live", "archived_at"])
    domain.record("timetable.timetable.archived", timetable)
    return timetable


@transaction.atomic
def copy_timetable(
    actor: Actor,
    source: Timetable,
    *,
    name: str,
    term_id: Any = None,
    effective_from: datetime.date | None = None,
    effective_to: datetime.date | None = None,
) -> Timetable:
    """A new draft with the source's periods and slots, in the same academic year (for the next term).

    Slots that could not be created today are left out: lessons whose teacher assignment has ended, and
    slots of archived sections. An archived room is dropped from the slot that booked it.
    """
    copy = create_timetable(
        actor,
        academic_year_id=source.academic_year_id,
        name=name,
        term_id=term_id,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    period_map: dict[Any, Period] = {}
    for period in source.periods.order_by("number"):
        period_map[period.pk] = Period.objects.create(
            school=actor.school,
            timetable=copy,
            number=period.number,
            name=period.name,
            start_time=period.start_time,
            end_time=period.end_time,
            is_break=period.is_break,
        )
    copied = skipped = 0
    for slot in source.slots.select_related("assignment", "section", "room"):
        ended = slot.assignment is not None and slot.assignment.status != AssignmentStatus.ACTIVE
        if ended or slot.section.status != RecordStatus.ACTIVE:
            skipped += 1
            continue
        target = period_map[slot.period_id]
        room_id = slot.room_id if slot.room and slot.room.status == RecordStatus.ACTIVE else None
        TimetableSlot.objects.create(
            school=actor.school,
            timetable=copy,
            period=target,
            weekday=slot.weekday,
            section_id=slot.section_id,
            kind=slot.kind,
            assignment_id=slot.assignment_id,
            title=slot.title,
            room_id=room_id,
            academic_year_id=copy.academic_year_id,
            effective_from=copy.effective_from,
            effective_to=copy.effective_to,
            is_live=False,
            start_time=target.start_time,
            end_time=target.end_time,
            staff_id=slot.staff_id,
            subject_id=slot.subject_id,
        )
        copied += 1
    domain.record("timetable.timetable.copied", copy, source=str(source.pk), slots=copied, skipped=skipped)
    return copy


# --------------------------------------------------------------------------------------------- periods
_PERIOD_CONFLICT = "This period number is taken, or its times overlap another period of the timetable."
_PERIOD_OR_CLASH = "The period number is taken, its times overlap another period, or its slots would clash."


def _period_timetable(actor: Actor, timetable_id: Any) -> Timetable:
    found = domain.resolve(Timetable, actor.school, timetable_id, "timetable_id", label="timetable")
    return _lock_editable(found.pk)


def _check_period_times(period: Period) -> None:
    if period.start_time >= period.end_time:
        raise ValidationError({"end_time": ["The end time must be after the start time."]})
    overlapping = Period.objects.filter(
        timetable_id=period.timetable_id, start_time__lt=period.end_time, end_time__gt=period.start_time
    ).exclude(pk=period.pk)
    if overlapping.exists():
        raise ValidationError({"start_time": ["This overlaps another period of the timetable."]})


@transaction.atomic
def create_period(actor: Actor, *, timetable_id: Any, **data: Any) -> Period:
    timetable = _period_timetable(actor, timetable_id)
    period = Period(school=actor.school, timetable=timetable, **data)
    _check_period_times(period)
    domain.save(period, conflict=_PERIOD_CONFLICT)
    domain.record("timetable.period.created", period, timetable=str(timetable.pk))
    return period


@transaction.atomic
def update_period(actor: Actor, period: Period, **data: Any) -> Period:
    timetable = _lock_editable(period.timetable_id)
    period = Period.objects.get(pk=period.pk)
    has_slots = period.slots.exists()
    if data.get("is_break") and has_slots:
        raise ValidationError({"is_break": ["Remove this period's slots before making it a break."]})
    changed = domain.apply_changes(period, data)
    if {"start_time", "end_time"} & set(changed):
        _check_period_times(period)
        if has_slots and timetable.is_live:
            _check_timetable_against_live(
                timetable, timetable.effective_from, timetable.effective_to, periods={period.pk: period}
            )
    if changed:
        # New times cascade to the period's slots (timetable migration 0002) and re-run the clash constraints.
        domain.save(period, conflict=_PERIOD_OR_CLASH, update_fields=changed)
        domain.record("timetable.period.updated", period, fields=changed)
    return period


@transaction.atomic
def delete_period(actor: Actor, period: Period) -> None:
    _lock_editable(period.timetable_id)
    if period.slots.exists():
        raise Conflict("This period still has slots. Remove them first.")
    domain.delete(period)
    domain.record("timetable.period.deleted", period)


# --------------------------------------------------------------------------------------------- slots
def _slot_period(actor: Actor, timetable: Timetable, period_id: Any) -> Period:
    period = domain.resolve(Period, actor.school, period_id, "period_id", label="period")
    if period.timetable_id != timetable.pk:
        raise ValidationError({"period_id": ["This period belongs to another timetable."]})
    if period.is_break:
        raise ValidationError({"period_id": ["Nothing can be scheduled in a break."]})
    return period


def _slot_room(actor: Actor, room_id: Any, section: Section) -> Room | None:
    if room_id is None:
        return None
    room = domain.resolve(Room, actor.school, room_id, "room_id", label="room")
    if room.status != RecordStatus.ACTIVE:
        raise ValidationError({"room_id": ["This room is archived."]})
    if room.campus_id and section.campus_id and room.campus_id != section.campus_id:
        raise ValidationError({"room_id": ["This room is on another campus than the section."]})
    return room


def _slot_assignment(actor: Actor, assignment_id: Any, section: Section) -> TeacherAssignment:
    if assignment_id is None:
        raise ValidationError({"assignment_id": ["A lesson needs a teacher assignment."]})
    assignment = domain.resolve(
        TeacherAssignment, actor.school, assignment_id, "assignment_id", label="teacher assignment"
    )
    if assignment.section_id != section.pk:
        raise ValidationError({"assignment_id": ["This assignment is for another section."]})
    if assignment.status != AssignmentStatus.ACTIVE:
        raise ValidationError({"assignment_id": ["This assignment has ended."]})
    if assignment.subject_id is None:
        raise ValidationError(
            {"assignment_id": ["A class-teacher-only assignment has no subject; use a subject assignment."]}
        )
    return assignment


def _apply_kind(slot: TimetableSlot, assignment: TeacherAssignment | None) -> None:
    """Set the assignment and its copies (lessons), or clear them (activities: callers refuse one)."""
    if slot.kind == SlotKind.LESSON:
        slot.assignment = assignment
        slot.staff_id = assignment.staff_id if assignment else None
        slot.subject_id = assignment.subject_id if assignment else None
    else:
        if not slot.title:
            raise ValidationError({"title": ["An activity needs a title (for example: Assembly)."]})
        slot.assignment = slot.staff = slot.subject = None


def _copy_from(slot: TimetableSlot, timetable: Timetable, period: Period) -> None:
    slot.academic_year_id = timetable.academic_year_id
    slot.effective_from, slot.effective_to = timetable.effective_from, timetable.effective_to
    slot.is_live = timetable.is_live
    slot.start_time, slot.end_time = period.start_time, period.end_time


@transaction.atomic
def create_slot(
    actor: Actor,
    *,
    timetable_id: Any,
    period_id: Any,
    weekday: int,
    section_id: Any,
    kind: str = SlotKind.LESSON,
    assignment_id: Any = None,
    title: str = "",
    room_id: Any = None,
) -> TimetableSlot:
    found = domain.resolve(Timetable, actor.school, timetable_id, "timetable_id", label="timetable")
    timetable = _lock_editable(found.pk)
    period = _slot_period(actor, timetable, period_id)
    section = domain.resolve(Section, actor.school, section_id, "section_id", label="section")
    if section.academic_year_id != timetable.academic_year_id:
        raise ValidationError({"section_id": ["This section belongs to another academic year."]})
    if section.status != RecordStatus.ACTIVE:
        raise ValidationError({"section_id": ["This section is archived."]})
    assignment = _slot_assignment(actor, assignment_id, section) if kind == SlotKind.LESSON else None
    if kind != SlotKind.LESSON and assignment_id is not None:
        raise ValidationError({"assignment_id": ["An activity has no teacher assignment."]})
    slot = TimetableSlot(
        school=actor.school,
        timetable=timetable,
        period=period,
        weekday=weekday,
        section=section,
        kind=kind,
        title=title,
        room=_slot_room(actor, room_id, section),
    )
    _apply_kind(slot, assignment)
    _copy_from(slot, timetable, period)
    _check_slot(slot)
    domain.save(slot, conflict=_CLASH)
    domain.record(
        "timetable.slot.created",
        slot,
        timetable=str(timetable.pk),
        section=str(section.pk),
        staff=str(slot.staff_id) if slot.staff_id else None,
    )
    return slot


@transaction.atomic
def update_slot(actor: Actor, slot: TimetableSlot, **data: Any) -> TimetableSlot:
    timetable = _lock_editable(slot.timetable_id)
    slot = TimetableSlot.objects.select_related("section").get(pk=slot.pk)
    before = {f: getattr(slot, f) for f in ("period_id", "weekday", "assignment_id", "room_id", "title")}
    period = _slot_period(actor, timetable, data["period_id"]) if "period_id" in data else slot.period
    slot.period = period
    if "weekday" in data:
        slot.weekday = data["weekday"]
    if "title" in data:
        slot.title = data["title"]
    if "room_id" in data:
        slot.room = _slot_room(actor, data["room_id"], slot.section)
    assignment = slot.assignment
    if "assignment_id" in data:
        assignment = (
            _slot_assignment(actor, data["assignment_id"], slot.section)
            if slot.kind == SlotKind.LESSON
            else None
        )
        if slot.kind != SlotKind.LESSON and data["assignment_id"] is not None:
            raise ValidationError({"assignment_id": ["An activity has no teacher assignment."]})
    _apply_kind(slot, assignment)
    _copy_from(slot, timetable, period)
    changed = [f for f, value in before.items() if getattr(slot, f) != value]
    if changed:
        _check_slot(slot)
        domain.save(slot, conflict=_CLASH)
        domain.record("timetable.slot.updated", slot, fields=changed)
    return slot


@transaction.atomic
def delete_slot(actor: Actor, slot: TimetableSlot) -> None:
    _lock_editable(slot.timetable_id)
    if slot.lessons.exists():
        raise Conflict("Lessons have been recorded for this slot. Archive the timetable instead.")
    domain.delete(slot)
    domain.record("timetable.slot.deleted", slot)


# --------------------------------------------------------------------------------------------- lessons
_LESSON_TAKEN = "This lesson is already recorded. Update it instead."


def _school_wide(actor: Actor) -> bool:
    return DataScope.SCHOOL in actor.scopes("lesson.manage")


def _check_lesson_rules(
    actor: Actor, slot: TimetableSlot, date: datetime.date, status: str, reason: str
) -> None:
    if slot.timetable.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Lessons of a closed academic year are history and cannot be changed.")
    if status == LessonStatus.HELD and date > school_today(actor):
        raise ValidationError({"status": ["A lesson in the future cannot be recorded as held."]})
    if reason and status != LessonStatus.CANCELLED:
        raise ValidationError({"cancellation_reason": ["Only a cancelled lesson has a cancellation reason."]})


def _ensure_own_teaching(
    actor: Actor, staff_membership_id: Any, assignment: TeacherAssignment | None
) -> None:
    """Without a school-wide grant, only the teacher of the class may record it, while they still teach it."""
    if _school_wide(actor):
        return
    if staff_membership_id != actor.membership.pk or assignment is None or assignment.status != "active":
        raise PermissionDenied("Only the teacher of this class can record its lessons.")


@transaction.atomic
def record_lesson(
    actor: Actor,
    slot: TimetableSlot,
    *,
    date: datetime.date,
    status: str = LessonStatus.HELD,
    topic: str = "",
    cancellation_reason: str = "",
) -> Lesson:
    slot = TimetableSlot.objects.select_related(
        "timetable__academic_year", "assignment", "staff", "section"
    ).get(pk=slot.pk)
    if slot.kind != SlotKind.LESSON or slot.staff_id is None or slot.subject_id is None:
        raise ValidationError({"slot_id": ["Only lesson slots have lessons."]})
    _ensure_own_teaching(actor, slot.staff.membership_id if slot.staff else None, slot.assignment)
    if not slot.is_live:
        raise ValidationError({"slot_id": ["This slot's timetable is not published."]})
    if not slot.effective_from <= date <= slot.effective_to or date.isoweekday() != slot.weekday:
        raise ValidationError({"date": ["This slot does not take place on that date."]})
    _check_lesson_rules(actor, slot, date, status, cancellation_reason)
    lesson = domain.save(
        Lesson(
            school=actor.school,
            slot=slot,
            date=date,
            section_id=slot.section_id,
            staff_id=slot.staff_id,
            subject_id=slot.subject_id,
            status=status,
            topic=topic,
            cancellation_reason=cancellation_reason,
            recorded_by=actor.membership,
        ),
        conflict=_LESSON_TAKEN,
    )
    domain.record("timetable.lesson.recorded", lesson, status=status, date=date.isoformat())
    return lesson


@transaction.atomic
def update_lesson(actor: Actor, lesson: Lesson, **data: Any) -> Lesson:
    lesson = Lesson.objects.select_related("slot__timetable__academic_year", "slot__assignment", "staff").get(
        pk=lesson.pk
    )
    _ensure_own_teaching(actor, lesson.staff.membership_id, lesson.slot.assignment)
    status = data.get("status", lesson.status)
    # A lesson that is no longer cancelled drops its old reason unless the client sends one (then: 400).
    default_reason = lesson.cancellation_reason if status == LessonStatus.CANCELLED else ""
    reason = data["cancellation_reason"] = data.get("cancellation_reason", default_reason)
    _check_lesson_rules(actor, lesson.slot, lesson.date, status, reason)
    changed = domain.apply_changes(lesson, data)
    if changed:
        lesson.recorded_by = actor.membership
        domain.save(lesson, conflict=_LESSON_TAKEN, update_fields=[*changed, "recorded_by"])
        domain.record("timetable.lesson.updated", lesson, fields=changed, status=lesson.status)
    return lesson
