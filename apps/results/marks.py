"""Marks entry: a subject teacher fills in one mark sheet (exam × subject) for their class."""

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.academics.models import Student, TeachingAssignment
from apps.accounts.models import MANAGEMENT_ROLES, Role
from apps.core.api import SchoolAPIView

from .models import ExamMark, MarkSheet
from .services import _pct, grade_for, sheet_is_published


def _can_edit(request, sheet: MarkSheet) -> bool:
    if request.roles & MANAGEMENT_ROLES:
        return True
    return TeachingAssignment.objects.filter(teacher=request.user, class_group=sheet.exam.class_group, subject=sheet.subject).exists()


def _sheet_or_404(request, sheet_id) -> MarkSheet:
    sheet = MarkSheet.objects.filter(id=sheet_id).select_related("exam", "exam__class_group", "subject").first()
    if sheet is None or not _can_edit(request, sheet):
        raise Http404
    return sheet


def _num(value: Decimal) -> float | int:
    return int(value) if value == value.to_integral_value() else float(value)


def sheet_summary(sheet: MarkSheet) -> dict:
    """How far along a sheet is, for lists: entered / total and the average so far."""
    total = Student.objects.filter(class_group=sheet.exam.class_group, is_active=True).count()
    rows = list(ExamMark.objects.filter(exam=sheet.exam, subject=sheet.subject, student__is_active=True).values("marks", "is_absent"))
    scored = [r["marks"] for r in rows if not r["is_absent"]]
    average = sum(scored, Decimal("0")) / len(scored) if scored else None
    percent = _pct(average, sheet.max_marks) if average is not None else None
    return {
        "entered": len(rows),
        "total": total,
        "average": round(float(average), 1) if average is not None else None,
        "percent": percent,
        "grade": grade_for(percent, sheet.school) if percent is not None else None,
    }


def _status(sheet: MarkSheet) -> str:
    """What the teacher sees: held for moderation still reads as submitted (it's with the exam cell)."""
    if sheet_is_published(sheet):
        return "published"
    return MarkSheet.Status.SUBMITTED if sheet.status == MarkSheet.Status.REVIEW else sheet.status


def sheet_card(sheet: MarkSheet) -> dict:
    exam = sheet.exam
    return {
        "id": str(sheet.id),
        "exam": {"id": str(exam.id), "name": exam.name, "held_on": exam.held_on.isoformat()},
        "subject": {"id": str(sheet.subject_id), "name": sheet.subject.name, "code": sheet.subject.code},
        "class": {"id": str(exam.class_group_id), "short_label": exam.class_group.short_label},
        "max_marks": _num(sheet.max_marks),
        "due_on": sheet.due_on.isoformat() if sheet.due_on else None,
        "status": _status(sheet),
        "saved_at": sheet.saved_at.isoformat() if sheet.saved_at else None,
        "submitted_at": sheet.submitted_at.isoformat() if sheet.submitted_at else None,
        **sheet_summary(sheet),
    }


def sheet_detail(request, sheet: MarkSheet) -> dict:
    students = Student.objects.filter(class_group=sheet.exam.class_group, is_active=True).order_by("roll_no", "full_name")
    marks = {m.student_id: m for m in ExamMark.objects.filter(exam=sheet.exam, subject=sheet.subject)}
    status = _status(sheet)

    def row(s):
        mark = marks.get(s.id)
        scored = mark is not None and not mark.is_absent
        pct = _pct(mark.marks, sheet.max_marks) if scored else None
        return {
            "id": str(s.id),
            "roll_no": s.roll_no,
            "name": s.full_name,
            "absent": bool(mark and mark.is_absent),
            "marks": _num(mark.marks) if scored else None,
            "percent": pct,
            "grade": grade_for(pct, sheet.school) if pct is not None else None,
        }

    return {**sheet_card(sheet), "editable": status == MarkSheet.Status.OPEN, "students": [row(s) for s in students]}


class TeacherMarkSheetsView(SchoolAPIView):
    """Mark sheets the exam cell has opened for the caller's subjects and classes."""

    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def get(self, request):
        pairs = TeachingAssignment.objects.filter(teacher=request.user).values_list("class_group_id", "subject_id")
        sheets = [
            s
            for s in MarkSheet.objects.filter(exam__class_group_id__in={c for c, _ in pairs})
            .select_related("exam", "exam__class_group", "subject")
            .order_by("due_on", "exam__held_on")
            if (s.exam.class_group_id, s.subject_id) in set(pairs)
        ]
        return Response({"sheets": [sheet_card(s) for s in sheets]})


class MarkSheetView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES
    permission = {"PUT": ("exams", "edit")}

    def get(self, request, sheet_id):
        return Response(sheet_detail(request, _sheet_or_404(request, sheet_id)))

    def put(self, request, sheet_id):
        """Autosave. Valid entries are stored; invalid ones come back in `errors` (by student id) untouched."""
        sheet = _sheet_or_404(request, sheet_id)
        if sheet_is_published(sheet):
            raise PermissionDenied("Results are published. Changes now need the principal's approval.")
        if sheet.status != MarkSheet.Status.OPEN:
            raise PermissionDenied("These marks are with the exam cell for review.")
        entries = request.data.get("entries")
        if not isinstance(entries, list):
            raise ValidationError({"entries": "Send a list of marks."})
        roster = {str(s.id): s for s in Student.objects.filter(class_group=sheet.exam.class_group, is_active=True)}
        errors: dict = {}
        with transaction.atomic():
            for entry in entries:
                sid = str(entry.get("student_id", ""))
                student = roster.get(sid)
                if student is None:
                    continue
                if entry.get("absent"):
                    ExamMark.objects.update_or_create(
                        exam=sheet.exam, student=student, subject=sheet.subject,
                        defaults={"marks": Decimal("0"), "max_marks": sheet.max_marks, "is_absent": True},
                    )
                    continue
                raw = entry.get("marks")
                if raw in (None, ""):
                    ExamMark.objects.filter(exam=sheet.exam, student=student, subject=sheet.subject).delete()
                    continue
                try:
                    value = Decimal(str(raw))
                except InvalidOperation:
                    errors[sid] = "Enter a number."
                    continue
                if value < 0:
                    errors[sid] = "Marks can't be negative."
                elif value > sheet.max_marks:
                    errors[sid] = f"Max is {_num(sheet.max_marks)} — recheck the answer sheet."
                elif value * 2 != (value * 2).to_integral_value():
                    errors[sid] = "Use whole or half marks."
                else:
                    ExamMark.objects.update_or_create(
                        exam=sheet.exam, student=student, subject=sheet.subject,
                        defaults={"marks": value, "max_marks": sheet.max_marks, "is_absent": False},
                    )
            sheet.saved_at = timezone.now()
            sheet.saved_by = request.user
            sheet.save(update_fields=["saved_at", "saved_by", "updated_at"])
        return Response({**sheet_detail(request, sheet), "errors": errors})


class MarkSheetSubmitView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES
    permission = ("exams", "edit")

    def post(self, request, sheet_id):
        sheet = _sheet_or_404(request, sheet_id)
        if sheet_is_published(sheet) or sheet.status != MarkSheet.Status.OPEN:
            raise PermissionDenied("These marks have already been submitted.")
        summary = sheet_summary(sheet)
        missing = summary["total"] - summary["entered"]
        if missing:
            raise ValidationError({"marks": f"{missing} marks still to enter. Mark absentees as AB."})
        sheet.status = MarkSheet.Status.SUBMITTED
        sheet.submitted_at = timezone.now()
        sheet.submitted_by = request.user
        sheet.save(update_fields=["status", "submitted_at", "submitted_by", "updated_at"])
        return Response(sheet_detail(request, sheet))


class MarkCorrectionView(SchoolAPIView):
    """After results are published, a change goes to the principal as a correction request."""

    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def post(self, request, sheet_id):
        from apps.approvals.services import open_request

        from .models import MarkCorrection

        sheet = _sheet_or_404(request, sheet_id)
        if not sheet_is_published(sheet):
            raise ValidationError({"sheet": "These marks aren't published yet. Edit them directly."})
        reason = str(request.data.get("reason", "")).strip()
        if not reason:
            raise ValidationError({"reason": "Say why the marks need to change."})
        current = {str(m.student_id): m for m in ExamMark.objects.filter(exam=sheet.exam, subject=sheet.subject, is_absent=False)}
        entries = []
        for e in request.data.get("entries") or []:
            sid = str(e.get("student_id", ""))
            mark = current.get(sid)
            if mark is None:
                raise ValidationError({"entries": "Only marked students can be corrected."})
            try:
                to = Decimal(str(e.get("marks")))
            except InvalidOperation as exc:
                raise ValidationError({"entries": "Enter a number."}) from exc
            if not 0 <= to <= sheet.max_marks:
                raise ValidationError({"entries": f"Marks must be between 0 and {_num(sheet.max_marks)}."})
            if to != mark.marks:
                entries.append({"student_id": sid, "from": _num(mark.marks), "to": _num(to), "note": str(e.get("note", ""))[:80]})
        if not entries:
            raise ValidationError({"entries": "Nothing changed."})
        corr = MarkCorrection.objects.create(exam=sheet.exam, subject=sheet.subject, reason=reason[:1000], entries=entries)
        open_request(
            kind="marks",
            target=corr,
            requested_by=request.user,
            summary=f"{sheet.exam.name} · {sheet.exam.class_group.short_label} {sheet.subject.name} · {len(entries)} change{'s' if len(entries) != 1 else ''}",
        )
        return Response({"pending_approval": True, "changes": len(entries)}, status=202)
