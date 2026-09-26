"""Console: academics page. Grades and sections, subject allocation, syllabus coverage and the homework load."""

from collections import Counter, defaultdict
from datetime import timedelta

from django.db import transaction
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import ClassGroup, Student, Subject, SubjectVacancy, TeachingAssignment, TimetableSlot
from apps.accounts.audit import audit
from apps.accounts.models import Membership, Role
from apps.core.api import SchoolAPIView, parse_uuid
from apps.core.utils import school_today
from apps.notifications.models import Category
from apps.notifications.services import notify
from apps.principal.views import _grade_key, _pct, staff_on_leave

from .common import CONSOLE_ROLES, current_term

# The school's academic policy lives in `school.settings["academics"]`; these are the defaults.
DEFAULT_POLICY = {"homework_limit": 12, "max_periods": 32, "section_capacity": 40}
# Syllabus coverage and homework are tracked for the core subjects, in the design's column order.
CORE_CODES = ["ENG", "MATH", "SCI", "SST", "HIN", "CS"]
# How far behind plan a subject may run before it is flagged.
BEHIND_POINTS = 10
TRAILING_POINTS = 7


def policy(school) -> dict:
    return {**DEFAULT_POLICY, **((school.settings or {}).get("academics") or {})}


def teachers() -> dict:
    """Active teachers by id."""
    return {m.user_id: m.user for m in Membership.objects.filter(role=Role.TEACHER, is_active=True).select_related("user")}


def teacher_loads() -> Counter:
    """Periods a week per teacher, from the live timetable."""
    return Counter(TimetableSlot.objects.exclude(teacher=None).values_list("teacher_id", flat=True))


def teacher_subjects() -> dict:
    """What each teacher is qualified to teach: every subject they teach somewhere."""
    out = defaultdict(set)
    for tid, sid in TeachingAssignment.objects.values_list("teacher_id", "subject_id"):
        out[tid].add(sid)
    return out


def busy_periods() -> dict:
    """(weekday, period) pairs each teacher already teaches."""
    out = defaultdict(set)
    for tid, wd, p in TimetableSlot.objects.exclude(teacher=None).values_list("teacher_id", "weekday", "period"):
        out[tid].add((wd, p))
    return out


def grade_label(grade: str) -> str:
    return grade if not grade.isdigit() else f"Grade {grade}"


def grade_range(grades: list[str]) -> str:
    """"6–10" for consecutive number grades, "7, 9" otherwise."""
    nums = sorted(int(g) for g in grades if g.isdigit())
    if nums and nums == list(range(nums[0], nums[-1] + 1)) and len(nums) > 1:
        return f"{nums[0]}–{nums[-1]}"
    return ", ".join(str(n) for n in nums) or ", ".join(grades)


def person(user) -> dict | None:
    return {"id": str(user.id), "name": user.full_name, "initials": user.initials} if user else None


# ------------------------------------------------------------------ allocation


def allocation_gaps(school) -> dict:
    """Every subject on a section's timetable needs a teacher. Unstaffed pairs are grouped by grade and subject,
    each with a qualified teacher who has the spare periods (and is free at those times) as a suggestion."""
    today = school_today(school)
    pol = policy(school)
    groups = {g.id: g for g in ClassGroup.objects.all()}
    slots = defaultdict(list)
    for gid, sid, wd, p in TimetableSlot.objects.values_list("class_group_id", "subject_id", "weekday", "period"):
        slots[(gid, sid)].append((wd, p))
    staffed = set(TeachingAssignment.objects.values_list("class_group_id", "subject_id"))
    required = set(slots)
    open_pairs = sorted(required - staffed, key=lambda k: (_grade_key(groups[k[0]].grade), groups[k[0]].section))
    subjects = {s.id: s for s in Subject.objects.all()}
    vacancies = {(v.class_group_id, v.subject_id): v for v in SubjectVacancy.objects.filter(filled_at=None)}

    grouped: dict = {}
    for gid, sid in open_pairs:
        g = groups[gid]
        vac = vacancies.get((gid, sid))
        key = (g.grade, sid, vac.reason if vac else "vacant")
        item = grouped.setdefault(key, {"grade": g.grade, "subject_id": sid, "reason": key[2], "since": None, "sections": [], "periods": []})
        item["sections"].append(g)
        item["periods"].append(len(slots[(gid, sid)]))
        if vac and (item["since"] is None or vac.since < item["since"]):
            item["since"] = vac.since

    loads = teacher_loads()
    quals = teacher_subjects()
    busy = busy_periods()
    staff = teachers()
    away = {lv.user_id for lv in staff_on_leave(today)}
    gaps = []
    for item in grouped.values():
        need = [wp for g in item["sections"] for wp in slots[(g.id, item["subject_id"])]]
        candidates = [
            tid
            for tid in staff
            if item["subject_id"] in quals.get(tid, set())
            and tid not in away
            and loads.get(tid, 0) + len(need) <= pol["max_periods"]
            and not (busy.get(tid, set()) & set(need))
            and len(set(need)) == len(need)
        ]
        best = min(candidates, key=lambda tid: (loads.get(tid, 0), staff[tid].full_name), default=None)
        subject = subjects[item["subject_id"]]
        gaps.append(
            {
                "key": f"{item['grade']}:{subject.id}",
                "grade": item["grade"],
                "subject": {"id": str(subject.id), "name": subject.name, "code": subject.code},
                "sections": [{"id": str(g.id), "section": g.section, "label": g.short_label} for g in item["sections"]],
                "periods": item["periods"][0] if len(set(item["periods"])) == 1 else sum(item["periods"]),
                "periods_each": len(set(item["periods"])) == 1,
                "reason": item["reason"],
                "since": item["since"].isoformat() if item["since"] else None,
                "suggestion": (
                    {
                        **person(staff[best]),
                        "subjects": sorted(subjects[s].name for s in quals[best] if s in subjects),
                        "load": loads.get(best, 0),
                        "max": pol["max_periods"],
                    }
                    if best
                    else None
                ),
            }
        )
    return {
        "required": len(required),
        "staffed": len(required & staffed),
        "unassigned": len(open_pairs),
        "percent": _pct(len(required & staffed), len(required)),
        "gaps": gaps,
    }


# ------------------------------------------------------------------ syllabus


def syllabus_coverage(school) -> dict:
    """Average syllabus covered per grade and core subject, from the teachers' progress logs, against their plan."""
    from apps.learning.models import SyllabusProgress
    from apps.results.models import ExamMark

    subjects = {s.code: s for s in Subject.objects.filter(code__in=CORE_CODES)}
    codes = [c for c in CORE_CODES if c in subjects]
    taught = set(TimetableSlot.objects.values_list("class_group__grade", "subject__code").distinct())
    cells = defaultdict(lambda: {"done": [], "plan": []})
    for grade, code, pct, plan in SyllabusProgress.objects.filter(subject__code__in=codes).values_list(
        "class_group__grade", "subject__code", "percent", "planned_percent"
    ):
        cells[(grade, code)]["done"].append(pct)
        if plan is not None:
            cells[(grade, code)]["plan"].append(plan)
    grades = sorted({g for g, _c in cells} | {g for g, c in taught if c in codes and g.isdigit()}, key=_grade_key)
    grades = [g for g in grades if g.isdigit()]
    rows, plans = [], []
    for grade in grades:
        row = []
        for code in codes:
            c = cells.get((grade, code))
            if not c or not c["done"]:
                row.append({"code": code, "percent": None, "planned": None, "taught": (grade, code) in taught})
                continue
            done = round(sum(c["done"]) / len(c["done"]))
            plan = round(sum(c["plan"]) / len(c["plan"])) if c["plan"] else None
            plans.extend(c["plan"])
            row.append({"code": code, "percent": done, "planned": plan, "taught": True, "behind": plan is not None and plan - done >= BEHIND_POINTS})
        rows.append({"grade": grade, "cells": row})
    plan_to_date = round(sum(plans) / len(plans)) if plans else None

    # The subject furthest behind plan, whether its last unit test fell too, and who else is trailing.
    flat = [(r["grade"], c) for r in rows for c in r["cells"] if c["percent"] is not None and c["planned"] is not None]
    alert = None
    if flat:
        grade, worst = max(flat, key=lambda gc: gc[1]["planned"] - gc[1]["percent"])
        gap = worst["planned"] - worst["percent"]
        if gap >= BEHIND_POINTS:
            drop = None
            exams = list(
                ExamMark.objects.filter(exam__class_group__grade=grade, subject=subjects[worst["code"]], is_absent=False)
                .values_list("exam__name", "exam__held_on")
                .distinct()
            )
            names = [n for n, _d in sorted({(n, d) for n, d in exams}, key=lambda x: x[1])]
            names = list(dict.fromkeys(names))
            if len(names) >= 2:
                avg = {}
                for name in names[-2:]:
                    rows_ = ExamMark.objects.filter(exam__class_group__grade=grade, exam__name=name, subject=subjects[worst["code"]], is_absent=False).values_list(
                        "marks", "max_marks"
                    )
                    vals = [float(m) * 100 / float(o) for m, o in rows_ if o]
                    avg[name] = round(sum(vals) / len(vals), 1) if vals else None
                a, b = avg.get(names[-2]), avg.get(names[-1])
                if a is not None and b is not None:
                    drop = {"from_exam": names[-2], "exam": names[-1], "from": a, "to": b, "delta": round(b - a)}
            trailing = defaultdict(list)
            for g, c in flat:
                if (g, c["code"]) != (grade, worst["code"]) and c["planned"] - c["percent"] >= TRAILING_POINTS:
                    trailing[c["code"]].append(g)
            alert = {
                "grade": grade,
                "subject": {"code": worst["code"], "name": subjects[worst["code"]].name},
                "percent": worst["percent"],
                "planned": worst["planned"],
                "gap": gap,
                "exam_drop": drop if drop and drop["delta"] < 0 else None,
                "trailing": [
                    {"code": code, "name": subjects[code].name, "grades": grade_range(gs)}
                    for code, gs in sorted(trailing.items(), key=lambda kv: -len(kv[1]))
                    if len(gs) >= 2
                ][:1],
            }
    return {
        "subjects": [{"code": c, "name": subjects[c].name} for c in codes],
        "rows": rows,
        "plan_to_date": plan_to_date,
        "alert": alert,
    }


# ------------------------------------------------------------------ homework


def homework_week(school) -> dict:
    """Homework set this week (Mon–Sat), averaged per section for each grade, against the policy limit."""
    from apps.homework.models import Homework
    from apps.results.models import ExamPaper

    today = school_today(school)
    monday = today - timedelta(days=today.weekday())
    saturday = monday + timedelta(days=5)
    limit = policy(school)["homework_limit"]
    sections = Counter(ClassGroup.objects.values_list("grade", flat=True))
    counts = Counter()
    due = defaultdict(Counter)
    for grade, due_on in Homework.objects.filter(assigned_on__gte=monday, assigned_on__lte=saturday).values_list("class_group__grade", "due_date"):
        counts[grade] += 1
        due[grade][due_on] += 1
    rows = []
    for grade in sorted((g for g in sections if g.isdigit()), key=_grade_key):
        avg = counts[grade] / sections[grade]
        rows.append({"grade": grade, "per_section": round(avg), "average": round(avg, 1), "total": counts[grade], "over": round(avg) > limit})
    over = [r for r in rows if r["over"]]
    worst = None
    if over:
        w = max(over, key=lambda r: r["per_section"])
        busiest, n = max(due[w["grade"]].items(), key=lambda kv: kv[1]) if due[w["grade"]] else (None, 0)
        worst = {"grade": w["grade"], "per_section": w["per_section"], "busiest_day": busiest.isoformat() if busiest else None, "busiest_count": round(n / sections[w["grade"]])}
    exam_soon = ExamPaper.objects.filter(date__gte=today, date__lte=today + timedelta(days=28)).exists()
    return {"week_start": monday.isoformat(), "week_end": saturday.isoformat(), "limit": limit, "rows": rows, "over": [r["grade"] for r in over], "worst": worst, "exam_soon": exam_soon}


# ------------------------------------------------------------------ grades and sections


def grades_and_sections(school) -> dict:
    pol = policy(school)
    sizes = Counter(Student.objects.filter(is_active=True).values_list("class_group_id", flat=True))
    groups = sorted(ClassGroup.objects.select_related("class_teacher"), key=lambda g: (_grade_key(g.grade), g.section))
    by_grade = defaultdict(list)
    for g in groups:
        by_grade[g.grade].append(g)
    gaps = allocation_gaps(school)["gaps"]
    gap_subjects = defaultdict(list)
    for gap in gaps:
        gap_subjects[gap["grade"]].append(gap["subject"]["name"])
    cap = pol["section_capacity"]
    out = []
    for grade, members in by_grade.items():
        sections = []
        for g in members:
            n = sizes.get(g.id, 0)
            sections.append({"id": str(g.id), "section": g.section, "label": g.short_label, "class_teacher": person(g.class_teacher), "students": n, "capacity": cap, "over": n > cap})
        out.append(
            {
                "grade": grade,
                "students": sum(s["students"] for s in sections),
                "capacity": cap * len(sections),
                "sections": sections,
                "gaps": sorted(set(gap_subjects.get(grade, []))),
                "no_class_teacher": [s["label"] for s in sections if not s["class_teacher"]],
                "over_capacity": [s["label"] for s in sections if s["over"]],
            }
        )
    return {"students": sum(sizes.values()), "sections": len(groups), "capacity": cap, "grades": out}


class AcademicsView(SchoolAPIView):
    """Everything on the academics page."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        from apps.results.models import ExamPaper

        school = request.school
        today = school_today(school)
        structure = grades_and_sections(school)
        first = ExamPaper.objects.filter(date__gte=today).order_by("date").select_related("exam").first()
        return Response(
            {
                "today": today.isoformat(),
                "term": current_term(school, today),
                "teachers": len(teachers()),
                "next_exam": {"name": first.exam.name, "date": first.date.isoformat()} if first else None,
                "policy": policy(school),
                "structure": structure,
                "allocation": allocation_gaps(school),
                "syllabus": syllabus_coverage(school),
                "homework": homework_week(school),
            }
        )


class AllocateView(SchoolAPIView):
    """Give a teacher an unstaffed subject in one or more sections: creates the teaching assignment and puts the
    teacher on those periods of the live timetable. Refused if the teacher is busy then or over the load limit."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        school = request.school
        subject = Subject.objects.filter(id=parse_uuid(request.data.get("subject_id"), "subject_id")).first()
        if subject is None:
            raise ValidationError({"subject_id": "Choose a subject."})
        ids = request.data.get("class_group_ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"class_group_ids": "Choose at least one section."})
        groups = list(ClassGroup.objects.filter(id__in=[parse_uuid(i, "class_group_ids") for i in ids]))
        if len(groups) != len(set(ids)):
            raise ValidationError({"class_group_ids": "Unknown section."})
        teacher = teachers().get(parse_uuid(request.data.get("teacher_id"), "teacher_id"))
        if teacher is None:
            raise ValidationError({"teacher_id": "Choose a teacher."})
        if TeachingAssignment.objects.filter(class_group__in=groups, subject=subject).exists():
            raise ValidationError({"class_group_ids": "One of these sections already has a teacher for this subject."})
        slots = list(TimetableSlot.objects.filter(class_group__in=groups, subject=subject))
        need = [(s.weekday, s.period) for s in slots]
        clash = set(need) & busy_periods().get(teacher.id, set())
        if clash or len(set(need)) != len(need):
            raise ValidationError({"teacher_id": f"{teacher.full_name} already teaches at one of these periods."})
        limit = policy(school)["max_periods"]
        load = teacher_loads().get(teacher.id, 0)
        if load + len(slots) > limit:
            raise ValidationError({"teacher_id": f"That would take {teacher.full_name} to {load + len(slots)} periods a week (limit {limit})."})
        with transaction.atomic():
            for g in groups:
                TeachingAssignment.objects.create(teacher=teacher, class_group=g, subject=subject)
            TimetableSlot.objects.filter(id__in=[s.id for s in slots]).update(teacher=teacher)
            SubjectVacancy.objects.filter(class_group__in=groups, subject=subject, filled_at=None).update(filled_at=timezone.now(), filled_by=request.user)
        labels = ", ".join(sorted(g.short_label for g in groups))
        notify(
            [teacher],
            school=school,
            category=Category.GENERAL,
            title=f"New classes: {subject.name}, {labels}",
            body=f"{len(slots)} periods a week, starting on your timetable now.",
            data={"type": "timetable"},
        )
        audit(request, "academics.allocate", target=teacher, summary=f"{subject.name} · {labels} → {teacher.full_name}")
        return Response(allocation_gaps(school), status=201)


class HomeworkNotifyView(SchoolAPIView):
    """Remind the teachers who set homework in over-limit grades this week."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        from apps.homework.models import Homework

        school = request.school
        week = homework_week(school)
        grades = request.data.get("grades") or week["over"]
        grades = [g for g in grades if g in week["over"]]
        if not grades:
            raise ValidationError({"grades": "No grade is over the homework limit this week."})
        monday = week["week_start"]
        ids = set(
            Homework.objects.filter(class_group__grade__in=grades, assigned_on__gte=monday, assigned_on__lte=week["week_end"])
            .exclude(assigned_by=None)
            .values_list("assigned_by_id", flat=True)
        )
        users = [u for tid, u in teachers().items() if tid in ids]
        label = " and ".join(grade_label(g) for g in grades)
        notify(
            users,
            school=school,
            category=Category.HOMEWORK,
            title=f"Homework load: {label}",
            body=f"{label} {'is' if len(grades) == 1 else 'are'} over the school's limit of {week['limit']} tasks a week. Please hold new tasks and spread due dates.",
            data={"type": "homework_load", "grades": grades},
        )
        audit(request, "academics.homework_reminder", summary=f"{label} · {len(users)} teachers")
        return Response({"sent_to": len(users)})


class SyllabusDetailView(SchoolAPIView):
    """Section-by-section syllabus progress for one grade (the "Syllabus plans" sheet)."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        from apps.learning.models import SyllabusProgress

        grade = request.query_params.get("grade") or ""
        rows = (
            SyllabusProgress.objects.filter(class_group__grade=grade, subject__code__in=CORE_CODES)
            .select_related("class_group", "subject", "updated_by")
            .order_by("class_group__section", "subject__name")
        )
        return Response(
            {
                "grade": grade,
                "items": [
                    {
                        "section": p.class_group.short_label,
                        "subject": p.subject.name,
                        "code": p.subject.code,
                        "percent": p.percent,
                        "planned": p.planned_percent,
                        "topic": p.current_topic,
                        "teacher": p.updated_by.full_name if p.updated_by else None,
                        "updated_at": p.updated_at.isoformat(),
                    }
                    for p in rows
                ],
            }
        )


class PolicyView(SchoolAPIView):
    """The homework limit, teaching load cap and section capacity (kept in school settings)."""

    allowed_roles = CONSOLE_ROLES

    def patch(self, request):
        school = request.school
        current = policy(school)
        for key in DEFAULT_POLICY:
            if key in request.data:
                try:
                    value = int(request.data[key])
                except (TypeError, ValueError) as exc:
                    raise ValidationError({key: "Enter a whole number."}) from exc
                if not 1 <= value <= 99:
                    raise ValidationError({key: "Use a number from 1 to 99."})
                current[key] = value
        school.settings = {**(school.settings or {}), "academics": current}
        school.save(update_fields=["settings"])
        audit(request, "academics.policy", summary=", ".join(f"{k} {v}" for k, v in current.items()))
        return Response(current)


urlpatterns = [
    path("console/academics", AcademicsView.as_view()),
    path("console/academics/allocate", AllocateView.as_view()),
    path("console/academics/homework/notify", HomeworkNotifyView.as_view()),
    path("console/academics/syllabus", SyllabusDetailView.as_view()),
    path("console/academics/policy", PolicyView.as_view()),
]
