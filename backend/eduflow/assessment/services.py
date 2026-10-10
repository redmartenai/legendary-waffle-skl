"""Exam writes and result calculations. Transactional and audited (``assessment.*``).

Teacher writes are narrow (a section and subject they teach, ADR-027): ``assessment.create`` enters and
submits marks, ``assessment.update`` requests corrections of published marks. Approval
(``assessment.approve``) and publication (``exam.manage``) are school-wide.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import AcademicYear, Section, Subject, Term
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people.models import StaffProfile, Student, TeacherAssignment
from eduflow.people.scoping import may_write
from eduflow.tenancy import clock, domain

from .models import (
    CorrectionStatus,
    Exam,
    GradeBand,
    Mark,
    MarkCorrection,
    MarkSheet,
    SheetStatus,
)

ENTER, CORRECT = "assessment.create", "assessment.update"
HUNDRED = Decimal(100)


# ------------------------------------------------------------------------------------------------ exams
def _exam_refs(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    if "academic_year_id" in data:
        data["academic_year"] = domain.resolve(
            AcademicYear, actor.school, data.pop("academic_year_id"), "academic_year_id"
        )
    if "term_id" in data:
        term_id = data.pop("term_id")
        data["term"] = domain.resolve(Term, actor.school, term_id, "term_id") if term_id else None
    return data


def _check_dates(exam: Exam) -> None:
    if exam.ends_on < exam.starts_on:
        raise ValidationError({"ends_on": ["The exam ends before it starts."]})
    if exam.marks_deadline < exam.ends_on:
        raise ValidationError({"marks_deadline": ["The marks deadline is before the exam ends."]})
    if exam.term is not None and exam.term.academic_year_id != exam.academic_year_id:
        raise ValidationError({"term_id": ["This term is in another academic year."]})


@transaction.atomic
def create_exam(actor: Actor, **data: Any) -> Exam:
    exam = Exam(school=actor.school, **_exam_refs(actor, data))
    _check_dates(exam)
    domain.save(exam, conflict="An exam with this name already exists in the academic year.")
    domain.record("assessment.exam.created", exam, name=exam.name)
    return exam


@transaction.atomic
def update_exam(actor: Actor, exam: Exam, **data: Any) -> Exam:
    if exam.published_at is not None:
        raise Conflict("This exam is published.")
    changed = domain.apply_changes(exam, _exam_refs(actor, data))
    _check_dates(exam)
    if changed:
        domain.save(exam, conflict="An exam with this name already exists in the academic year.")
        domain.record("assessment.exam.updated", exam, fields=changed)
    return exam


# ------------------------------------------------------------------------------------------------ sheets
@transaction.atomic
def create_sheet(
    actor: Actor,
    exam: Exam,
    *,
    section_id: Any,
    subject_id: Any,
    max_marks: Decimal,
    pass_marks: Decimal | None = None,
    teacher_id: Any = None,
) -> MarkSheet:
    section = domain.resolve(Section, actor.school, section_id, "section_id", label="section")
    if section.academic_year_id != exam.academic_year_id:
        raise ValidationError({"section_id": ["This section is in another academic year."]})
    subject = domain.resolve(Subject, actor.school, subject_id, "subject_id", label="subject")
    teacher = domain.resolve(StaffProfile, actor.school, teacher_id, "teacher_id") if teacher_id else None
    if pass_marks is not None and pass_marks > max_marks:
        raise ValidationError({"pass_marks": ["Pass marks are above the maximum."]})
    sheet = MarkSheet(
        school=actor.school,
        exam=exam,
        section=section,
        subject=subject,
        teacher=teacher,
        max_marks=max_marks,
        pass_marks=pass_marks,
    )
    domain.save(sheet, conflict="This exam already has a mark sheet for that section and subject.")
    domain.record("assessment.mark_sheet.created", sheet, exam=str(exam.pk))
    return sheet


@transaction.atomic
def generate_sheets(
    actor: Actor, exam: Exam, *, max_marks: Decimal, pass_marks: Decimal | None = None
) -> list[MarkSheet]:
    """One sheet per active subject-teacher assignment of the exam's year (existing sheets are kept)."""
    if pass_marks is not None and pass_marks > max_marks:
        raise ValidationError({"pass_marks": ["Pass marks are above the maximum."]})
    assignments = TeacherAssignment.objects.filter(
        school_id=actor.school.pk, academic_year=exam.academic_year, status="active", subject__isnull=False
    )
    existing = set(exam.sheets.values_list("section_id", "subject_id"))
    created = []
    for a in assignments:
        if a.subject_id is None or (a.section_id, a.subject_id) in existing:
            continue
        existing.add((a.section_id, a.subject_id))
        created.append(
            MarkSheet.objects.create(
                school=actor.school,
                exam=exam,
                section_id=a.section_id,
                subject_id=a.subject_id,
                teacher_id=a.staff_id,
                max_marks=max_marks,
                pass_marks=pass_marks,
            )
        )
    domain.record("assessment.exam.sheets_generated", exam, created=len(created))
    return created


def roster(sheet: MarkSheet) -> QuerySet[Student]:
    return Student.objects.filter(
        school_id=sheet.school_id, enrollments__section_id=sheet.section_id, enrollments__status="active"
    ).distinct()


def _narrow(actor: Actor, permission: str, sheet: MarkSheet) -> None:
    if not may_write(actor, permission, sheet.section, sheet.subject):
        raise PermissionDenied("You can work only on mark sheets of a section and subject you teach.")


def _locked(sheet: MarkSheet) -> MarkSheet:
    return MarkSheet.objects.select_for_update(of=("self",)).select_related("exam").get(pk=sheet.pk)


def _value(sheet: MarkSheet, marks: Decimal | None, absent: bool, field: str) -> None:
    if absent and marks is not None:
        raise ValidationError({field: ["An absent student has no marks."]})
    if not absent and marks is None:
        raise ValidationError({field: ["Give the marks, or mark the student absent."]})
    if marks is not None and marks > sheet.max_marks:
        raise ValidationError({field: [f"Marks are above the maximum ({sheet.max_marks})."]})


@transaction.atomic
def enter_marks(actor: Actor, sheet: MarkSheet, entries: Iterable[dict[str, Any]]) -> MarkSheet:
    _narrow(actor, ENTER, sheet)
    sheet = _locked(sheet)
    if sheet.status != SheetStatus.DRAFT:
        raise Conflict(f"This mark sheet is {sheet.status}; marks change only through a correction.")
    enrolled = set(roster(sheet).values_list("pk", flat=True))
    rows = list(entries)
    for i, entry in enumerate(rows):
        if entry["student_id"] not in enrolled:
            raise ValidationError({f"marks[{i}].student_id": ["This student is not in the class."]})
        _value(sheet, entry.get("marks"), entry.get("absent", False), f"marks[{i}].marks")
    for entry in rows:
        Mark.objects.update_or_create(
            school_id=sheet.school_id,
            sheet=sheet,
            student_id=entry["student_id"],
            defaults={
                "marks": entry.get("marks"),
                "absent": entry.get("absent", False),
                "remark": entry.get("remark", ""),
            },
        )
    domain.record("assessment.mark_sheet.marks_entered", sheet, count=len(rows))
    return sheet


@transaction.atomic
def submit_sheet(actor: Actor, sheet: MarkSheet) -> MarkSheet:
    _narrow(actor, ENTER, sheet)
    sheet = _locked(sheet)
    if sheet.status != SheetStatus.DRAFT:
        raise Conflict(f"This mark sheet is already {sheet.status}.")
    missing = roster(sheet).exclude(marks__sheet=sheet).count()
    if missing:
        raise ValidationError({"marks": [f"{missing} student(s) have no marks yet."]})
    sheet.status, sheet.submitted_at, sheet.submitted_by = (
        SheetStatus.SUBMITTED,
        timezone.now(),
        actor.membership,
    )
    sheet.return_note = ""
    sheet.save()
    on_time = clock.local(sheet.submitted_at, actor.school).date() <= sheet.exam.marks_deadline
    domain.record("assessment.mark_sheet.submitted", sheet, on_time=on_time)
    return sheet


@transaction.atomic
def decide_sheet(actor: Actor, sheet: MarkSheet, decision: str, note: str = "") -> MarkSheet:
    sheet = _locked(sheet)
    if sheet.status != SheetStatus.SUBMITTED:
        raise Conflict("Only a submitted mark sheet can be approved or returned.")
    sheet.decided_by, sheet.decided_at = actor.membership, timezone.now()
    if decision == "approve":
        sheet.status = SheetStatus.APPROVED
    else:
        sheet.status, sheet.return_note = SheetStatus.DRAFT, note or "Returned for changes"
    sheet.save()
    domain.record("assessment.mark_sheet.decided", sheet, decision=decision, note=note or None)
    if decision != "approve" and sheet.submitted_by is not None:
        notifications.notify(
            actor.school,
            [sheet.submitted_by],
            kind="approval",
            title="Mark sheet returned",
            body=sheet.return_note,
            link=("mark_sheet", sheet.pk),
        )
    return sheet


@transaction.atomic
def publish_exam(actor: Actor, exam: Exam) -> int:
    """Publish every approved sheet of the exam and notify the families of the students concerned."""
    now = timezone.now()
    sheets = list(exam.sheets.select_for_update().filter(status=SheetStatus.APPROVED))
    if not sheets:
        raise Conflict("No approved mark sheets to publish.")
    for sheet in sheets:
        sheet.status, sheet.published_at = SheetStatus.PUBLISHED, now
        sheet.save(update_fields=["status", "published_at", "updated_at"])
    if exam.published_at is None:
        exam.published_at = now
        exam.save(update_fields=["published_at", "updated_at"])
    domain.record("assessment.exam.published", exam, sheets=len(sheets))
    students = Student.objects.filter(marks__sheet__in=sheets).distinct()
    for student in students:
        notifications.notify(
            actor.school,
            notifications.family_of(student),
            kind="marks",
            title=f"{exam.name} results published",
            student=student,
            link=("exam", exam.pk),
        )
    return len(sheets)


# ------------------------------------------------------------------------------------------------ corrections
@transaction.atomic
def request_correction(
    actor: Actor, mark: Mark, *, reason: str, new_marks: Decimal | None = None, new_absent: bool = False
) -> MarkCorrection:
    sheet = mark.sheet
    _narrow(actor, CORRECT, sheet)
    if sheet.status not in (SheetStatus.APPROVED, SheetStatus.PUBLISHED):
        raise Conflict("Change marks directly while the sheet is a draft; submitted sheets can be returned.")
    _value(sheet, new_marks, new_absent, "new_marks")
    if mark.marks == new_marks and mark.absent == new_absent:
        raise ValidationError({"new_marks": ["This is the current value."]})
    correction = MarkCorrection(
        school=actor.school,
        mark=mark,
        old_marks=mark.marks,
        old_absent=mark.absent,
        new_marks=new_marks,
        new_absent=new_absent,
        reason=reason,
        requested_by=actor.membership,
    )
    domain.save(correction, conflict="A correction of this mark is already pending.")
    domain.record("assessment.mark_correction.requested", correction, mark=str(mark.pk))
    return correction


@transaction.atomic
def decide_correction(
    actor: Actor, correction: MarkCorrection, decision: str, note: str = ""
) -> MarkCorrection:
    correction = (
        MarkCorrection.objects.select_for_update(of=("self",)).select_related("mark").get(pk=correction.pk)
    )
    if correction.status != CorrectionStatus.PENDING:
        raise Conflict("This correction has already been decided.")
    correction.decided_by, correction.decided_at, correction.decision_note = (
        actor.membership,
        timezone.now(),
        note,
    )
    if decision == "approve":
        mark = Mark.objects.select_for_update().get(pk=correction.mark_id)
        mark.marks, mark.absent = correction.new_marks, correction.new_absent
        mark.save()
        correction.status = CorrectionStatus.APPROVED
    else:
        correction.status = CorrectionStatus.DECLINED
    correction.save()
    domain.record("assessment.mark_correction.decided", correction, decision=decision)
    notifications.notify(
        actor.school,
        [correction.requested_by],
        kind="approval",
        title=f"Marks correction {correction.status}",
        body=note,
        link=("mark_correction", correction.pk),
    )
    return correction


# ------------------------------------------------------------------------------------------------ results
def percent(marks: Decimal | None, max_marks: Decimal) -> Decimal | None:
    if marks is None:
        return None
    return (marks * HUNDRED / max_marks).quantize(Decimal("0.01"), ROUND_HALF_UP)


def band_for(school: Any, pct: Decimal | None, bands: list[GradeBand] | None = None) -> str | None:
    if pct is None:
        return None
    scale = bands if bands is not None else list(GradeBand.objects.filter(school_id=school.pk))
    for band in sorted(scale, key=lambda b: b.min_percent, reverse=True):
        if pct >= band.min_percent:
            return band.label
    return None


def report_card(student: Student, exam: Exam, marks: QuerySet[Mark]) -> dict[str, Any]:
    """Subject lines and totals of one student for one exam, from the given (already scoped) marks."""
    bands = list(GradeBand.objects.filter(school_id=student.school_id))
    lines, total, total_max = [], Decimal(0), Decimal(0)
    for mark in (
        marks.filter(student=student, sheet__exam=exam)
        .select_related("sheet__subject")
        .order_by("sheet__subject__name")
    ):
        sheet = mark.sheet
        pct = percent(mark.marks, sheet.max_marks)
        lines.append(
            {
                "subject": sheet.subject,
                "marks": mark.marks,
                "absent": mark.absent,
                "max_marks": sheet.max_marks,
                "percent": pct,
                "grade": band_for(student.school, pct, bands),
                "passed": None if sheet.pass_marks is None else (mark.marks or 0) >= sheet.pass_marks,
                "remark": mark.remark,
            }
        )
        total += mark.marks or 0
        total_max += sheet.max_marks
    overall = percent(total, total_max) if total_max else None
    return {
        "student": student,
        "exam": exam,
        "lines": lines,
        "total": total,
        "max_total": total_max,
        "percent": overall,
        "grade": band_for(student.school, overall, bands),
    }


# ------------------------------------------------------------------------------------------------ monitoring
def overdue_sheets(school: Any, today: datetime.date) -> QuerySet[MarkSheet]:
    """Sheets still in draft after their exam's marks deadline ("Marks overdue")."""
    return MarkSheet.objects.filter(
        school_id=school.pk, status=SheetStatus.DRAFT, exam__marks_deadline__lt=today
    ).select_related("exam", "section", "subject", "teacher__membership__user")


def exam_percentages(school: Any) -> dict[Any, list[tuple[Any, Decimal]]]:
    """Per student: (exam, percentage over all non-draft subjects) in exam order (trend inputs)."""
    rows = (
        Mark.objects.filter(school_id=school.pk, absent=False)
        .exclude(sheet__status=SheetStatus.DRAFT)
        .values_list("student_id", "sheet__exam_id", "sheet__exam__starts_on", "marks", "sheet__max_marks")
    )
    sums: dict[tuple[Any, Any], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    dates: dict[Any, datetime.date] = {}
    for student_id, exam_id, starts_on, value, max_marks in rows:
        entry = sums[(student_id, exam_id)]
        entry[0] += value or 0
        entry[1] += max_marks
        dates[exam_id] = starts_on
    series: dict[Any, list[tuple[Any, Decimal]]] = defaultdict(list)
    for (student_id, exam_id), (got, out_of) in sums.items():
        series[student_id].append((exam_id, got * HUNDRED / out_of))
    for values in series.values():
        values.sort(key=lambda pair: dates[pair[0]])
    return dict(series)


def trend(series: list[tuple[Any, Decimal]]) -> tuple[Decimal, Decimal, Decimal] | None:
    """(before, now, delta): the latest exam against the mean of the earlier ones (prototype marksTrend)."""
    if len(series) < 2:
        return None
    earlier = [p for _, p in series[:-1]]
    before = sum(earlier, Decimal(0)) / len(earlier)
    now = series[-1][1]
    return before, now, now - before
