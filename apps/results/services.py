from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg
from django.utils import timezone

from apps.academics.models import Student, TeachingAssignment
from apps.accounts.models import Membership, Role

from .models import Exam, ExamMark, MarkSheet, ReportCardNote

# CBSE-style nine-point scale; schools can swap in their board's scheme.
GRADE_SCALE = [(91, "A1"), (81, "A2"), (71, "B1"), (61, "B2"), (51, "C1"), (41, "C2"), (33, "D"), (0, "E")]
# Letter grades with pluses, used by many state-board and ICSE schools (and the EduFlow designs).
LETTER_PLUS_SCALE = [(90, "A+"), (80, "A"), (70, "B+"), (60, "B"), (50, "C+"), (40, "C"), (33, "D"), (0, "E")]

SCALES = {"cbse9": GRADE_SCALE, "letter_plus": LETTER_PLUS_SCALE}


def scale_for(school) -> list[tuple[int, str]]:
    name = ((school.settings or {}).get("grading") if school is not None else None) or "cbse9"
    return SCALES.get(name, GRADE_SCALE)


def grade_for(percent: float, school=None) -> str:
    return next(grade for threshold, grade in scale_for(school) if percent >= threshold)


def _pct(marks: Decimal, max_marks: Decimal) -> float:
    return round(float(marks) * 100 / float(max_marks), 1) if max_marks else 0.0


def _principal_name(school) -> str | None:
    member = Membership.objects.filter(school=school, role=Role.PRINCIPAL, is_active=True).select_related("user").first()
    return member.user.full_name if member else None


def sheet_is_published(sheet: MarkSheet) -> bool:
    """Published to families. Once the exam cell publishes subject by subject, each sheet's own status decides;
    before that (older exams), publishing the exam publishes every subject."""
    if sheet.status == MarkSheet.Status.PUBLISHED:
        return True
    return sheet.exam.is_published and not MarkSheet.objects.filter(exam_id=sheet.exam_id, status=MarkSheet.Status.PUBLISHED).exists()


def family_visibility(exams) -> tuple[list, callable]:
    """The exams families can see, and a check for whether one subject of an exam is out yet."""
    sheets = {(s.exam_id, s.subject_id): s.status for s in MarkSheet.objects.filter(exam__in=exams)}
    by_subject = {eid for (eid, _sid), st in sheets.items() if st == MarkSheet.Status.PUBLISHED}
    by_id = {e.id: e for e in exams}

    def visible(exam_id, subject_id) -> bool:
        if exam_id in by_subject:
            status = sheets.get((exam_id, subject_id))
            return status == MarkSheet.Status.PUBLISHED if status else by_id[exam_id].is_published
        return by_id[exam_id].is_published

    return [e for e in exams if e.is_published or e.id in by_subject], visible


def student_results(student: Student) -> dict:
    school = student.school
    group = student.class_group
    all_exams = list(Exam.objects.filter(class_group=group).order_by("held_on"))
    exams, visible = family_visibility(all_exams)
    marks = [m for m in ExamMark.objects.filter(exam__in=exams, student=student, is_absent=False).select_related("subject") if visible(m.exam_id, m.subject_id)]
    by_exam: dict = defaultdict(list)
    for mark in marks:
        by_exam[mark.exam_id].append(mark)

    averages = {
        (row["exam_id"], row["subject_id"]): row["avg"]
        for row in ExamMark.objects.filter(exam__in=exams, is_absent=False).values("exam_id", "subject_id").annotate(avg=Avg("marks"))
        if visible(row["exam_id"], row["subject_id"])
    }
    # Class average for the whole paper: the mean of every student's overall percentage.
    overall: dict = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    per_student: dict = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    for row in ExamMark.objects.filter(exam__in=exams, is_absent=False).values("exam_id", "subject_id", "student_id", "marks", "max_marks"):
        if not visible(row["exam_id"], row["subject_id"]):
            continue
        acc = per_student[(row["exam_id"], row["student_id"])]
        acc[0] += row["marks"]
        acc[1] += row["max_marks"]
    for (exam_id, _sid), (got, out_of) in per_student.items():
        bucket = overall[exam_id]
        bucket[0] += Decimal(str(_pct(got, out_of)))
        bucket[1] += 1
    teachers = {
        a.subject_id: a.teacher.full_name
        for a in TeachingAssignment.objects.filter(class_group=group).select_related("teacher")
    }
    notes = {
        n.exam_id: {"body": n.body, "author": n.author.full_name if n.author else None, "on": timezone.localdate(n.created_at).isoformat()}
        for n in ReportCardNote.objects.filter(exam__in=exams, student=student).select_related("author")
    }

    exam_payloads, trend = [], []
    subject_series: dict = defaultdict(list)
    for exam in exams:
        rows = sorted(by_exam.get(exam.id, []), key=lambda m: m.subject.name)
        if not rows:
            continue
        total = sum((m.marks for m in rows), Decimal("0"))
        max_total = sum((m.max_marks for m in rows), Decimal("0"))
        percent = _pct(total, max_total)
        subjects = []
        for mark in rows:
            pct = _pct(mark.marks, mark.max_marks)
            class_avg = averages.get((exam.id, mark.subject_id))
            subjects.append(
                {
                    "subject": mark.subject.name,
                    "code": mark.subject.code,
                    "color": mark.subject.color,
                    "teacher": teachers.get(mark.subject_id),
                    "marks": float(mark.marks),
                    "max_marks": float(mark.max_marks),
                    "percent": pct,
                    "grade": grade_for(pct, school),
                    "class_average": round(float(class_avg) * 100 / float(mark.max_marks), 1) if class_avg else None,
                }
            )
            subject_series[mark.subject.name].append({"exam": exam.name, "percent": pct})
        got, count = overall[exam.id]
        class_avg = round(float(got) / float(count), 1) if count else None
        exam_payloads.append(
            {
                "id": str(exam.id),
                "name": exam.name,
                "held_on": exam.held_on.isoformat(),
                "published_on": exam.published_on.isoformat() if exam.published_on else None,
                "total": float(total),
                "max_total": float(max_total),
                "percent": percent,
                "grade": grade_for(percent, school),
                "class_average": class_avg,
                "class_average_grade": grade_for(class_avg, school) if class_avg is not None else None,
                "note": notes.get(exam.id),
                "subjects": subjects,
            }
        )
        trend.append({"exam": exam.name, "percent": percent})

    upcoming = [
        {"id": str(e.id), "name": e.name, "held_on": e.held_on.isoformat(), "results_on": e.results_on.isoformat() if e.results_on else None}
        for e in all_exams
        if e not in exams
    ]

    return {
        "student": {
            "id": str(student.id),
            "name": student.full_name,
            "class": group.short_label,
            "roll_no": student.roll_no,
            "admission_no": student.admission_no,
        },
        "school": {"name": school.name, "principal": _principal_name(school)},
        "class_teacher": group.class_teacher.full_name if group.class_teacher else None,
        "exams": list(reversed(exam_payloads)),
        "upcoming": upcoming,
        "trend": trend,
        "subjects": [{"subject": name, "points": points} for name, points in sorted(subject_series.items())],
    }
