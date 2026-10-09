"""Academic-structure writes. Each runs in a transaction, validates every reference inside the actor's school,
and is audited (``academics.<entity>.<created|updated|deleted>``)."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal
from rest_framework.exceptions import ValidationError

from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.tenancy import domain

from .models import (
    AcademicYear,
    AcademicYearStatus,
    Campus,
    Department,
    Grade,
    RecordStatus,
    Room,
    Section,
    Subject,
    Term,
)

# Sent inside the closing transaction, before the year is saved as closed. Modules holding year-bound records
# (people: enrollments, teacher assignments) wind them down, so nobody keeps access through a finished year.
year_closing = Signal()  # kwargs: actor, year

# ---------------------------------------------------------------------------------------- simple entities
_DUPLICATE = "A record with that name or code already exists in this school."


def _create(actor: Actor, model: type[Any], data: dict[str, Any]) -> Any:
    obj = domain.save(model(school=actor.school, **data), conflict=_DUPLICATE)
    domain.record(f"academics.{domain.entity(model)}.created", obj, fields=sorted(data))
    return obj


def _update(obj: Any, data: dict[str, Any]) -> Any:
    changed = domain.apply_changes(obj, data)
    if changed:
        domain.save(obj, conflict=_DUPLICATE, update_fields=changed)
        domain.record(f"academics.{domain.entity(obj)}.updated", obj, fields=changed)
    return obj


def _delete(obj: Any) -> None:
    name = domain.entity(obj)
    domain.delete(obj)
    domain.record(f"academics.{name}.deleted", obj)


@transaction.atomic
def create_campus(actor: Actor, **data: Any) -> Campus:
    return _create(actor, Campus, data)  # type: ignore[no-any-return]


@transaction.atomic
def update_campus(actor: Actor, campus: Campus, **data: Any) -> Campus:
    return _update(campus, data)  # type: ignore[no-any-return]


@transaction.atomic
def create_department(actor: Actor, **data: Any) -> Department:
    return _create(actor, Department, data)  # type: ignore[no-any-return]


@transaction.atomic
def update_department(actor: Actor, department: Department, **data: Any) -> Department:
    return _update(department, data)  # type: ignore[no-any-return]


@transaction.atomic
def create_grade(actor: Actor, **data: Any) -> Grade:
    return _create(actor, Grade, data)  # type: ignore[no-any-return]


@transaction.atomic
def update_grade(actor: Actor, grade: Grade, **data: Any) -> Grade:
    return _update(grade, data)  # type: ignore[no-any-return]


def _subject_refs(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    if "department_id" in data:
        dept_id = data.pop("department_id")
        data["department"] = (
            domain.resolve(Department, actor.school, dept_id, "department_id") if dept_id else None
        )
    return data


@transaction.atomic
def create_subject(actor: Actor, **data: Any) -> Subject:
    return _create(actor, Subject, _subject_refs(actor, data))  # type: ignore[no-any-return]


@transaction.atomic
def update_subject(actor: Actor, subject: Subject, **data: Any) -> Subject:
    return _update(subject, _subject_refs(actor, data))  # type: ignore[no-any-return]


delete_campus = delete_department = delete_grade = delete_subject = transaction.atomic(_delete)


# --------------------------------------------------------------------------------------------- academic years
_YEAR_TRANSITIONS = {
    AcademicYearStatus.PLANNED: {AcademicYearStatus.ACTIVE},
    AcademicYearStatus.ACTIVE: {AcademicYearStatus.CLOSED},
    AcademicYearStatus.CLOSED: set(),
}


def _check_dates(actor: Actor, start: Any, end: Any, exclude: Any = None) -> None:
    if start >= end:
        raise ValidationError({"end_date": ["The end date must be after the start date."]})
    overlapping = AcademicYear.objects.for_school(actor.school).filter(
        start_date__lte=end, end_date__gte=start
    )
    if exclude is not None:
        overlapping = overlapping.exclude(pk=exclude)
    if overlapping.exists():
        raise ValidationError({"start_date": ["This overlaps another academic year of the school."]})


def _make_current(year: AcademicYear) -> None:
    AcademicYear.objects.for_school(year.school_id).filter(is_current=True).exclude(pk=year.pk).update(
        is_current=False
    )


@transaction.atomic
def create_academic_year(actor: Actor, **data: Any) -> AcademicYear:
    _check_dates(actor, data["start_date"], data["end_date"])
    year = domain.save(
        AcademicYear(school=actor.school, **data),
        conflict="This overlaps another academic year, or the name is taken.",
    )
    domain.record("academics.academic_year.created", year, fields=sorted(data))
    return year


@transaction.atomic
def update_academic_year(actor: Actor, year: AcademicYear, **data: Any) -> AcademicYear:
    if year.status == AcademicYearStatus.CLOSED:
        raise Conflict("A closed academic year cannot be changed.")
    status = data.pop("status", None)
    if status is not None and status != year.status:
        if status not in _YEAR_TRANSITIONS[AcademicYearStatus(year.status)]:
            raise ValidationError({"status": [f"An academic year cannot go from {year.status} to {status}."]})
        data["status"] = status
        if status == AcademicYearStatus.CLOSED:
            data["is_current"] = False
    if data.get("is_current") and data.get("status", year.status) != AcademicYearStatus.ACTIVE:
        raise ValidationError({"is_current": ["Only an active academic year can be the current one."]})
    start, end = data.get("start_date", year.start_date), data.get("end_date", year.end_date)
    if "start_date" in data or "end_date" in data:
        _check_dates(actor, start, end, exclude=year.pk)
    if data.get("is_current"):
        _make_current(year)
    if data.get("status") == AcademicYearStatus.CLOSED:
        year_closing.send(sender=AcademicYear, actor=actor, year=year)
    changed = domain.apply_changes(year, data)
    if changed:
        domain.save(
            year, conflict="This overlaps another academic year, or the name is taken.", update_fields=changed
        )
        domain.record("academics.academic_year.updated", year, fields=changed, status=data.get("status"))
    return year


@transaction.atomic
def delete_academic_year(actor: Actor, year: AcademicYear) -> None:
    if year.status != AcademicYearStatus.PLANNED:
        raise Conflict("Only a planned academic year can be deleted. Close it instead.")
    _delete(year)


# --------------------------------------------------------------------------------------------- sections
def _open_year(actor: Actor, year_id: Any) -> AcademicYear:
    year = domain.resolve(AcademicYear, actor.school, year_id, "academic_year_id", label="academic year")
    if year.status == AcademicYearStatus.CLOSED:
        raise ValidationError({"academic_year_id": ["This academic year is closed."]})
    return year


def _active(obj: Any, field: str) -> Any:
    if getattr(obj, "status", RecordStatus.ACTIVE) != RecordStatus.ACTIVE:
        raise ValidationError({field: ["This record is archived."]})
    return obj


@transaction.atomic
def create_section(actor: Actor, **data: Any) -> Section:
    year = _open_year(actor, data.pop("academic_year_id"))
    grade = _active(
        domain.resolve(Grade, actor.school, data.pop("grade_id"), "grade_id", label="grade"), "grade_id"
    )
    campus_id = data.pop("campus_id", None)
    campus = (
        _active(domain.resolve(Campus, actor.school, campus_id, "campus_id"), "campus_id")
        if campus_id
        else None
    )
    section = domain.save(
        Section(school=actor.school, academic_year=year, grade=grade, campus=campus, **data),
        conflict="This grade already has a section with that name or code in this academic year.",
    )
    domain.record("academics.section.created", section, academic_year=str(year.pk), grade=str(grade.pk))
    return section


@transaction.atomic
def update_section(actor: Actor, section: Section, **data: Any) -> Section:
    if section.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Sections of a closed academic year cannot be changed.")
    if "campus_id" in data:
        campus_id = data.pop("campus_id")
        data["campus"] = (
            _active(domain.resolve(Campus, actor.school, campus_id, "campus_id"), "campus_id")
            if campus_id
            else None
        )
    capacity = data.get("capacity")
    if capacity is not None:
        # Same lock as enrollment, so a concurrent enrollment cannot slip in after the count.
        section = (
            Section.objects.select_for_update(of=("self",)).select_related("academic_year").get(pk=section.pk)
        )
        enrolled = section.enrollments.filter(status="active").count()
        if capacity < enrolled:
            raise ValidationError(
                {"capacity": [f"{enrolled} students are enrolled; capacity cannot be lower."]}
            )
    changed = domain.apply_changes(section, data)
    if changed:
        domain.save(
            section,
            conflict="This grade already has a section with that name or code in this academic year.",
            update_fields=changed,
        )
        domain.record("academics.section.updated", section, fields=changed)
    return section


@transaction.atomic
def delete_section(section: Section) -> None:
    if section.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Sections of a closed academic year are history and cannot be deleted.")
    _delete(section)


# --------------------------------------------------------------------------------------------- terms
_TERM_CONFLICT = "This overlaps another term of the academic year, or the name or code is taken."


def _check_term_dates(year: AcademicYear, start: Any, end: Any, exclude: Any = None) -> None:
    if start >= end:
        raise ValidationError({"end_date": ["The end date must be after the start date."]})
    if start < year.start_date or end > year.end_date:
        raise ValidationError({"start_date": ["A term must lie within its academic year."]})
    overlapping = Term.objects.filter(academic_year=year, start_date__lte=end, end_date__gte=start)
    if exclude is not None:
        overlapping = overlapping.exclude(pk=exclude)
    if overlapping.exists():
        raise ValidationError({"start_date": ["This overlaps another term of the academic year."]})


@transaction.atomic
def create_term(actor: Actor, **data: Any) -> Term:
    year = _open_year(actor, data.pop("academic_year_id"))
    _check_term_dates(year, data["start_date"], data["end_date"])
    term = domain.save(Term(school=actor.school, academic_year=year, **data), conflict=_TERM_CONFLICT)
    domain.record("academics.term.created", term, academic_year=str(year.pk))
    return term


@transaction.atomic
def update_term(actor: Actor, term: Term, **data: Any) -> Term:
    if term.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Terms of a closed academic year cannot be changed.")
    if "start_date" in data or "end_date" in data:
        _check_term_dates(
            term.academic_year,
            data.get("start_date", term.start_date),
            data.get("end_date", term.end_date),
            exclude=term.pk,
        )
    changed = domain.apply_changes(term, data)
    if changed:
        domain.save(term, conflict=_TERM_CONFLICT, update_fields=changed)
        domain.record("academics.term.updated", term, fields=changed)
    return term


@transaction.atomic
def delete_term(term: Term) -> None:
    if term.academic_year.status == AcademicYearStatus.CLOSED:
        raise Conflict("Terms of a closed academic year are history and cannot be deleted.")
    _delete(term)


# --------------------------------------------------------------------------------------------- rooms
def _room_refs(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    if "campus_id" in data:
        campus_id = data.pop("campus_id")
        data["campus"] = (
            _active(domain.resolve(Campus, actor.school, campus_id, "campus_id"), "campus_id")
            if campus_id
            else None
        )
    return data


@transaction.atomic
def create_room(actor: Actor, **data: Any) -> Room:
    return _create(actor, Room, _room_refs(actor, data))  # type: ignore[no-any-return]


@transaction.atomic
def update_room(actor: Actor, room: Room, **data: Any) -> Room:
    return _update(room, _room_refs(actor, data))  # type: ignore[no-any-return]


delete_room = transaction.atomic(_delete)
