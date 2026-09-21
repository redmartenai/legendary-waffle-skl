from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg

from apps.academics.models import Student

from .models import Exam, ExamMark

# CBSE-style nine-point scale; schools can swap in their board's scheme.
GRADE_SCALE = [(91, "A1"), (81, "A2"), (71, "B1"), (61, "B2"), (51, "C1"), (41, "C2"), (33, "D"), (0, "E")]


def grade_for(percent: float) -> str:
    return next(grade for threshold, grade in GRADE_SCALE if percent >= threshold)


def _pct(marks: Decimal, max_marks: Decimal) -> float:
    return round(float(marks) * 100 / float(max_marks), 1) if max_marks else 0.0


def student_results(student: Student) -> dict:
    exams = list(Exam.objects.filter(class_group=student.class_group, is_published=True))
    marks = ExamMark.objects.filter(exam__in=exams, student=student).select_related("subject")
    by_exam: dict = defaultdict(list)
    for mark in marks:
        by_exam[mark.exam_id].append(mark)

    averages = {
        (row["exam_id"], row["subject_id"]): row["avg"]
        for row in ExamMark.objects.filter(exam__in=exams).values("exam_id", "subject_id").annotate(avg=Avg("marks"))
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
                    "color": mark.subject.color,
                    "marks": float(mark.marks),
                    "max_marks": float(mark.max_marks),
                    "percent": pct,
                    "grade": grade_for(pct),
                    "class_average": round(float(class_avg) * 100 / float(mark.max_marks), 1) if class_avg else None,
                }
            )
            subject_series[mark.subject.name].append({"exam": exam.name, "percent": pct})
        exam_payloads.append(
            {
                "id": str(exam.id),
                "name": exam.name,
                "held_on": exam.held_on.isoformat(),
                "total": float(total),
                "max_total": float(max_total),
                "percent": percent,
                "grade": grade_for(percent),
                "subjects": subjects,
            }
        )
        trend.append({"exam": exam.name, "percent": percent})

    return {
        "student": {"id": str(student.id), "name": student.full_name},
        "exams": list(reversed(exam_payloads)),
        "trend": trend,
        "subjects": [{"subject": name, "points": points} for name, points in sorted(subject_series.items())],
    }
