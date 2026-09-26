"""Console: examinations page. Marks-entry status for the term's exam, the publish checklist, moderation,
the biggest drop since the last exam, and the next exam's date sheet with invigilators."""

import csv
import io
from collections import Counter, defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import Min
from django.http import Http404, HttpResponse
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import AcademicYear, ClassGroup, Student, StudentGuardian, Subject, TeachingAssignment
from apps.accounts.audit import audit
from apps.core.api import SchoolAPIView, parse_uuid
from apps.core.utils import school_today, school_tz
from apps.notifications.models import Category
from apps.notifications.services import notify
from apps.principal.views import _grade_key, staff_on_leave
from apps.results.models import Exam, ExamMark, ExamPaper, ExamSeries, Invigilation, MarkCorrection, MarkSheet, ReportCardNote

from .academics import grade_range, person, teachers
from .common import CONSOLE_ROLES, current_term

SUBJECT_ORDER = ["MATH", "SCI", "ENG", "SST", "HIN", "CS", "ECO"]
STATES = ["not_started", "progress", "review", "submitted", "published"]  # worst first


def _terms(school) -> list[dict]:
    return sorted((school.settings or {}).get("terms") or [], key=lambda t: t.get("starts_on", ""))


def _series_order() -> list[str]:
    """Exam names in the order they were first held."""
    rows = Exam.objects.values("name").annotate(first=Min("held_on")).order_by("first")
    return [r["name"] for r in rows]


def _pick_series(school, term: dict | None) -> str | None:
    """The exam whose marks the page tracks: the latest in the term that has mark sheets, else the latest held."""
    exams = Exam.objects.all()
    if term:
        exams = exams.filter(held_on__gte=term["starts_on"], held_on__lte=term["ends_on"])
    with_sheets = [n for n in _series_order() if exams.filter(name=n, mark_sheets__isnull=False).exists()]
    if with_sheets:
        return with_sheets[-1]
    names = [n for n in _series_order() if exams.filter(name=n).exists()]
    return names[-1] if names else None


def _pending_corrections(name: str) -> list:
    """Marks corrections on this exam still waiting for the principal, with their approval request."""
    from apps.approvals.models import ApprovalRequest
    from apps.approvals.services import request_for

    out = []
    for corr in MarkCorrection.objects.filter(exam__name=name, applied_at=None).select_related("exam__class_group", "subject").order_by("created_at"):
        req = request_for(corr)
        if req and req.status == ApprovalRequest.Status.PENDING:
            out.append((corr, req))
    return out


# ------------------------------------------------------------------ the marks matrix


def marks_matrix(school, name: str) -> dict:
    today = school_today(school)
    exams = list(Exam.objects.filter(name=name).select_related("class_group"))
    sheets = defaultdict(dict)
    for s in MarkSheet.objects.filter(exam__name=name).select_related("subject"):
        sheets[s.exam_id][s.subject.code] = s
    tracked = [e for e in exams if sheets.get(e.id)]
    codes = sorted({c for e in tracked for c in sheets[e.id]}, key=lambda c: SUBJECT_ORDER.index(c) if c in SUBJECT_ORDER else 99)
    reopened = Counter()
    for corr, _req in _pending_corrections(name):
        reopened[(corr.exam_id, corr.subject.code)] += len(corr.entries)
    sizes = Counter(Student.objects.filter(is_active=True).values_list("class_group_id", flat=True))
    entered = Counter()
    for eid, code in ExamMark.objects.filter(exam__in=tracked, student__is_active=True).values_list("exam_id", "subject__code"):
        entered[(eid, code)] += 1
    away = {lv.user_id for lv in staff_on_leave(today)}
    teacher_of = {(a.class_group_id, a.subject.code): a.teacher_id for a in TeachingAssignment.objects.select_related("subject")}
    names = dict(Subject.objects.filter(code__in=codes).values_list("code", "name"))

    def section_state(exam, code):
        s = sheets[exam.id].get(code)
        if s is None:
            return ("published", {"date": exam.published_on}) if exam.is_published else ("not_started", {})
        if s.status == MarkSheet.Status.PUBLISHED:
            if reopened[(exam.id, code)]:
                return "review", {"reopened": reopened[(exam.id, code)]}
            return "published", {"date": timezone.localtime(s.published_at, school_tz(school)).date() if s.published_at else exam.published_on}
        if s.status == MarkSheet.Status.REVIEW:
            return "review", {"note": s.review_note, "sheet": s}
        if s.status == MarkSheet.Status.SUBMITTED:
            return "submitted", {"date": timezone.localtime(s.submitted_at, school_tz(school)).date() if s.submitted_at else None}
        n = entered[(exam.id, code)]
        return ("progress" if n else "not_started"), {"due": s.due_on}

    by_grade = defaultdict(list)
    for e in tracked:
        by_grade[e.class_group.grade].append(e)
    rows, counts = [], Counter()
    for grade in sorted(by_grade, key=_grade_key):
        cells = []
        for code in codes:
            parts = [(e, *section_state(e, code)) for e in by_grade[grade] if code in sheets[e.id] or e.is_published]
            if not parts:
                cells.append({"code": code, "state": None})
                continue
            worst = min(parts, key=lambda p: STATES.index(p[1]))
            state, info = worst[1], worst[2]
            cell = {"code": code, "state": state, "sections": [e.class_group.short_label for e, st, _i in parts if st == state]}
            if state in ("published", "submitted"):
                dates = [i.get("date") for _e, st, i in parts if st == state and i.get("date")]
                cell["date"] = max(dates).isoformat() if dates else None
            elif state == "review":
                if info.get("reopened"):
                    cell["note"] = {"kind": "reopened", "count": info["reopened"]}
                else:
                    cell["note"] = {"kind": "text", "text": info.get("note") or ""}
                cell["sheet_ids"] = [str(i["sheet"].id) for _e, st, i in parts if st == "review" and i.get("sheet")]
            else:
                exams_ = [e for e, _st, _i in parts]
                total = sum(sizes.get(e.class_group_id, 0) for e in exams_)
                cell["entered"] = sum(entered[(e.id, code)] for e in exams_)
                cell["total"] = total
                if state == "not_started":
                    cell["teacher_on_leave"] = any(teacher_of.get((e.class_group_id, code)) in away for e in exams_)
                    dues = [i.get("due") for _e, st, i in parts if i.get("due")]
                    cell["due"] = min(dues).isoformat() if dues else None
            counts[state] += 1
            cells.append(cell)
        done = sum(1 for c in cells if c["state"] == "published")
        rows.append({"grade": grade, "cells": cells, "published": done, "total": sum(1 for c in cells if c["state"])})
    untracked = sorted({e.class_group.grade for e in exams if e not in tracked}, key=_grade_key)
    complete = [g for g in untracked if all(e.is_published for e in exams if e.class_group.grade == g)]
    return {
        "subjects": [{"code": c, "name": names.get(c, c)} for c in codes],
        "rows": rows,
        "counts": {s: counts[s] for s in STATES},
        "papers": sum(counts.values()),
        "grades": grade_range([r["grade"] for r in rows]) if rows else "",
        "complete": _ranges(complete),
        "max_marks": _common_max(tracked),
    }


def _ranges(grades: list[str]) -> list[str]:
    """["1–5", "11–12"] from ["1", …, "5", "11", "12"]."""
    nums = sorted(int(g) for g in grades if g.isdigit())
    out, run = [], []
    for n in nums:
        if run and n != run[-1] + 1:
            out.append(run)
            run = []
        run.append(n)
    if run:
        out.append(run)
    return [f"{r[0]}–{r[-1]}" if len(r) > 1 else str(r[0]) for r in out]


def _common_max(exams) -> int | None:
    values = set(MarkSheet.objects.filter(exam__in=exams).values_list("max_marks", flat=True))
    return int(values.pop()) if len(values) == 1 else None


# ------------------------------------------------------------------ checklist


def checklist(school, name: str, matrix: dict) -> dict:
    series = ExamSeries.objects.filter(name=name).select_related("template_approved_by").first()
    tz = school_tz(school)
    counts = matrix["counts"]
    total = matrix["papers"]
    term = None
    first = Exam.objects.filter(name=name).order_by("held_on").first()
    if first:
        term = current_term(school, first.held_on)
    items = []
    if series and series.grading_locked_at:
        items.append({"key": "grading", "state": "ok", "weightage": series.weightage, "term": term["name"] if term else None})
    else:
        items.append({"key": "grading", "state": "fail", "weightage": series.weightage if series else None, "term": term["name"] if term else None})
    if series and series.template_approved_at:
        by = series.template_approved_by
        items.append({"key": "template", "state": "ok", "by": by.full_name if by else None, "by_id": str(by.id) if by else None, "on": timezone.localtime(series.template_approved_at, tz).date().isoformat()})
    else:
        items.append({"key": "template", "state": "fail"})
    submitted = counts["submitted"] + counts["review"] + counts["published"]
    items.append(
        {"key": "submitted", "state": "ok" if submitted == total else "warn", "done": submitted, "total": total, "progress": counts["progress"], "not_started": counts["not_started"]}
    )
    pending = _pending_corrections(name)
    waiting = sum(1 for c, _r in pending for e in c.entries if not e.get("decision"))
    in_review = MarkSheet.objects.filter(exam__name=name, status=MarkSheet.Status.REVIEW).count()
    first_label = f"{pending[0][0].subject.name} {pending[0][0].exam.class_group.short_label}" if pending else None
    items.append({"key": "moderation", "state": "ok" if not waiting and not in_review else "warn", "corrections": waiting, "first": first_label, "sheets": in_review})
    # Class-teacher remarks, for the sections whose results aren't all out yet.
    scope = [
        e
        for e in Exam.objects.filter(name=name).select_related("class_group")
        if MarkSheet.objects.filter(exam=e).exclude(status=MarkSheet.Status.PUBLISHED).exists()
    ]
    sizes = Counter(Student.objects.filter(is_active=True, class_group__in=[e.class_group for e in scope]).values_list("class_group_id", flat=True))
    notes = Counter(ReportCardNote.objects.filter(exam__in=scope, student__is_active=True).values_list("exam__class_group_id", flat=True))
    done = sum(1 for e in scope if sizes.get(e.class_group_id) and notes.get(e.class_group_id, 0) >= sizes[e.class_group_id])
    items.append({"key": "remarks", "state": "ok" if done == len(scope) else "todo", "done": done, "total": len(scope), "grades": grade_range(sorted({e.class_group.grade for e in scope}, key=_grade_key))})
    notified = series.parents_notified_at if series else None
    items.append({"key": "notify", "state": "ok" if notified and counts["submitted"] == 0 else "todo", "at": notified.isoformat() if notified else None})
    releasable = MarkSheet.objects.filter(exam__name=name, status=MarkSheet.Status.SUBMITTED).count()
    failures = [i["key"] for i in items if i["state"] == "fail"]
    return {
        "items": items,
        "ok": sum(1 for i in items if i["state"] == "ok"),
        "total": len(items),
        "failures": failures,
        "release": {"papers": counts["submitted"], "sheets": releasable, "held": counts["review"]},
        "can_publish": not failures and releasable > 0,
    }


# ------------------------------------------------------------------ moderation


def moderation(name: str) -> dict:
    items = []
    students = {}
    pending = _pending_corrections(name)
    ids = [e["student_id"] for c, _r in pending for e in c.entries]
    students = {str(s.id): s for s in Student.objects.filter(id__in=ids)}
    for corr, req in pending:
        items.append(
            {
                "id": str(corr.id),
                "approval_id": str(req.id),
                "teacher": person(req.requested_by),
                "class": corr.exam.class_group.short_label,
                "subject": corr.subject.name,
                "sent_at": req.created_at.isoformat(),
                "published_on": corr.exam.published_on.isoformat() if corr.exam.published_on else None,
                "reason": corr.reason,
                "entries": [
                    {
                        "index": i,
                        "student": students[e["student_id"]].full_name if e["student_id"] in students else "",
                        "roll_no": students[e["student_id"]].roll_no if e["student_id"] in students else None,
                        "from": e["from"],
                        "to": e["to"],
                        "note": e.get("note", ""),
                        "decision": e.get("decision"),
                    }
                    for i, e in enumerate(corr.entries)
                ],
            }
        )
    sheets = [
        {"id": str(s.id), "class": s.exam.class_group.short_label, "subject": s.subject.name, "note": s.review_note}
        for s in MarkSheet.objects.filter(exam__name=name, status=MarkSheet.Status.REVIEW).select_related("exam__class_group", "subject").order_by("exam__class_group__grade")
    ]
    return {"count": sum(len(i["entries"]) for i in items) + len(sheets), "corrections": items, "sheets": sheets}


# ------------------------------------------------------------------ the biggest drop


def _avg(**filters) -> float | None:
    rows = ExamMark.objects.filter(is_absent=False, **filters).values_list("marks", "max_marks")
    vals = [float(m) * 100 / float(o) for m, o in rows if o]
    return round(sum(vals) / len(vals), 1) if vals else None


def biggest_drop(name: str, grades: list[str]) -> dict | None:
    """The grade × subject whose class average fell most since the previous exam."""
    from apps.learning.models import SyllabusProgress

    order = [n for n in _series_order() if ExamMark.objects.filter(exam__name=n).exists()]
    if name not in order or order.index(name) == 0:
        return None
    prev = order[order.index(name) - 1]
    codes = set(ExamMark.objects.filter(exam__name=name).values_list("subject__code", flat=True))
    worst = None
    for grade in grades:
        for code in codes:
            a = _avg(exam__name=prev, exam__class_group__grade=grade, subject__code=code)
            b = _avg(exam__name=name, exam__class_group__grade=grade, subject__code=code)
            if a is None or b is None:
                continue
            if worst is None or b - a < worst[2]:
                worst = (grade, code, b - a, a, b)
    if worst is None or worst[2] >= 0:
        return None
    grade, code, delta, a, b = worst
    series = order[: order.index(name) + 1]
    subject = ExamMark.objects.filter(subject__code=code).select_related("subject").first().subject
    sections = []
    for g in sorted(ClassGroup.objects.filter(grade=grade), key=lambda g: g.section):
        x = _avg(exam__name=prev, exam__class_group=g, subject__code=code)
        y = _avg(exam__name=name, exam__class_group=g, subject__code=code)
        if x is not None and y is not None:
            sections.append({"label": g.short_label, "id": str(g.id), "from": round(x), "to": round(y), "delta": round(y) - round(x)})
    school_points = [_avg(exam__name=n, subject__code=code, exam__class_group__grade__in=[str(i) for i in range(1, 13)]) for n in series]
    progress = list(SyllabusProgress.objects.filter(class_group__grade=grade, subject__code=code).values_list("percent", "planned_percent"))
    return {
        "grade": grade,
        "subject": {"code": code, "name": subject.name},
        "delta": round(b) - round(a),
        "exams": series,
        "grade_points": [_avg(exam__name=n, exam__class_group__grade=grade, subject__code=code) for n in series],
        "school_points": school_points,
        "sections": sections,
        "syllabus": {
            "percent": round(sum(p for p, _pl in progress) / len(progress)) if progress else None,
            "planned": round(sum(pl for _p, pl in progress if pl is not None) / max(1, sum(1 for _p, pl in progress if pl is not None))) if any(pl is not None for _p, pl in progress) else None,
        },
    }


# ------------------------------------------------------------------ the next exam's date sheet


def upcoming(school) -> dict | None:
    today = school_today(school)
    first = ExamPaper.objects.filter(date__gte=today).order_by("date").select_related("exam").first()
    if first is None:
        return None
    name = first.exam.name
    series = ExamSeries.objects.filter(name=name).first()
    per_room = series.invigilators_per_room if series else 2
    papers = list(ExamPaper.objects.filter(exam__name=name).select_related("exam__class_group", "subject").order_by("date", "starts_at"))
    duty = Counter(Invigilation.objects.filter(paper__in=papers).values_list("paper_id", flat=True))
    groups = defaultdict(list)
    for p in papers:
        groups[(p.date, p.subject_id, p.starts_at, p.ends_at)].append(p)
    rows = []
    for (d, _sid, starts, ends), ps in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][2], _grade_key(kv[1][0].exam.class_group.grade))):
        grades = sorted({p.exam.class_group.grade for p in ps}, key=_grade_key)
        needed = per_room * len(ps)
        assigned = sum(min(per_room, duty.get(p.id, 0)) for p in ps)
        rows.append(
            {
                "key": f"{d.isoformat()}:{ps[0].subject.code}:{starts:%H%M}-{ends:%H%M}:{grades[0]}",
                "date": d.isoformat(),
                "grades": grade_range(grades),
                "subject": {"code": ps[0].subject.code, "name": ps[0].subject.name},
                "starts_at": starts.strftime("%H:%M"),
                "ends_at": ends.strftime("%H:%M"),
                "rooms": len({p.room or p.id for p in ps}),
                "needed": needed,
                "assigned": assigned,
                "status": "complete" if assigned >= needed else "none" if assigned == 0 else "short",
            }
        )
    dates = sorted({p.date for p in papers})
    strip, d = [], dates[0]
    while d <= dates[-1]:
        if d.weekday() != 6:
            codes = []
            for p in papers:
                if p.date == d and p.subject.code not in codes:
                    codes.append(p.subject.code)
            strip.append({"date": d.isoformat(), "subjects": codes, "senior_only": bool(codes) and all(p.exam.class_group.grade in ("11", "12") for p in papers if p.date == d)})
        d += timedelta(days=1)
    with_papers = {p.exam.class_group.grade for p in papers}
    in_class = sorted({g for g in Exam.objects.filter(name=name).values_list("class_group__grade", flat=True)} - with_papers, key=_grade_key)
    return {
        "name": name,
        "first": dates[0].isoformat(),
        "last": dates[-1].isoformat(),
        "days": (dates[0] - today).days,
        "grades": grade_range(sorted(with_papers, key=_grade_key)),
        "in_class": _ranges(in_class),
        "per_room": per_room,
        "strip": strip,
        "rows": rows,
        "papers": len(rows),
    }


class ExamsView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        today = school_today(school)
        terms = _terms(school)
        wanted = request.query_params.get("term")
        term = next((t for t in terms if t["name"] == wanted), None) if wanted else current_term(school, today)
        if wanted and term is None:
            raise ValidationError({"term": "Unknown term."})
        name = _pick_series(school, term)
        matrix = marks_matrix(school, name) if name else None
        current_year = AcademicYear.objects.filter(is_current=True).first()
        return Response(
            {
                "today": today.isoformat(),
                "terms": [{"name": t["name"], "starts_on": t["starts_on"], "ends_on": t["ends_on"]} for t in terms],
                "term": term["name"] if term else None,
                "academic_year": current_year.name if current_year else None,
                "series": name,
                "matrix": matrix,
                "checklist": checklist(school, name, matrix) if name else None,
                "moderation": moderation(name) if name else None,
                "trend": biggest_drop(name, [r["grade"] for r in matrix["rows"]]) if name and matrix["rows"] else None,
                "upcoming": upcoming(school),
            }
        )


def _series_name(request) -> str:
    name = str(request.data.get("series") or "").strip()
    if not name or not Exam.objects.filter(name=name).exists():
        raise ValidationError({"series": "Choose an exam."})
    return name


class PublishResultsView(SchoolAPIView):
    """Release every submitted mark sheet of the exam to families. Refused while the checklist has failures."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        school = request.school
        name = _series_name(request)
        matrix = marks_matrix(school, name)
        check = checklist(school, name, matrix)
        if check["failures"]:
            raise ValidationError({"checklist": "Lock the grading scale and approve the report-card template before publishing."})
        sheets = list(MarkSheet.objects.filter(exam__name=name, status=MarkSheet.Status.SUBMITTED).select_related("exam__class_group", "subject"))
        if not sheets:
            raise ValidationError({"series": "There are no submitted marks to publish."})
        now = timezone.now()
        today = school_today(school)
        with transaction.atomic():
            MarkSheet.objects.filter(id__in=[s.id for s in sheets]).update(status=MarkSheet.Status.PUBLISHED, published_at=now, published_by=request.user)
            exams = {s.exam for s in sheets}
            for exam in exams:
                # The whole exam counts as published once every subject is out.
                if not MarkSheet.objects.filter(exam=exam).exclude(status=MarkSheet.Status.PUBLISHED).exists():
                    exam.is_published = True
                    exam.published_on = exam.published_on or today
                    exam.save(update_fields=["is_published", "published_on", "updated_at"])
            series, _ = ExamSeries.objects.get_or_create(name=name)
            series.published_at, series.published_by, series.parents_notified_at = now, request.user, now
            series.save(update_fields=["published_at", "published_by", "parents_notified_at", "updated_at"])
        by_group = defaultdict(list)
        for s in sheets:
            by_group[s.exam.class_group_id].append(s.subject.name)
        students = list(Student.objects.filter(class_group_id__in=by_group, is_active=True).select_related("user"))
        guardians = defaultdict(set)
        for link in StudentGuardian.objects.filter(student__in=students, receives_alerts=True).select_related("user", "student"):
            guardians[link.student.class_group_id].add(link.user)
        for s in students:
            if s.user:
                guardians[s.class_group_id].add(s.user)
        for gid, subjects in by_group.items():
            notify(
                list(guardians.get(gid, [])),
                school=school,
                category=Category.RESULTS,
                title=f"{name} results are out",
                body=", ".join(sorted(subjects))[:180],
                data={"type": "results", "exam": name},
            )
        audit(request, "exams.publish", summary=f"{name} · {len(sheets)} mark sheets · {len(by_group)} sections", detail={"sheets": [str(s.id) for s in sheets]})
        return Response({"published": len(sheets), "sections": len(by_group), "notified": sum(len(v) for v in guardians.values())})


class SeriesView(SchoolAPIView):
    """POST {lock_grading: true} or {approve_template: true} for one exam."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        name = _series_name(request)
        series, _ = ExamSeries.objects.get_or_create(name=name)
        if request.data.get("lock_grading"):
            series.grading_locked_at, series.grading_locked_by = timezone.now(), request.user
            audit(request, "exams.grading_locked", target=series, summary=name)
        elif request.data.get("approve_template"):
            series.template_approved_at, series.template_approved_by = timezone.now(), request.user
            audit(request, "exams.template_approved", target=series, summary=name)
        else:
            raise ValidationError({"action": "Nothing to change."})
        series.save()
        return Response({"ok": True})


def _correction_or_404(correction_id):
    from apps.approvals.models import ApprovalRequest
    from apps.approvals.services import request_for

    corr = MarkCorrection.objects.filter(id=parse_uuid(correction_id)).select_related("exam__class_group", "subject").first()
    req = request_for(corr) if corr else None
    if corr is None or req is None:
        raise Http404
    if req.status != ApprovalRequest.Status.PENDING:
        raise ValidationError({"status": "This correction has already been decided."})
    return corr, req


class CorrectionEntryView(SchoolAPIView):
    """POST {decision: accept|reject} on one changed mark. When every change has a decision, the correction goes
    through the approvals flow: approved with the accepted changes, or declined if none were accepted."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, correction_id, index):
        from apps.approvals.services import decide

        corr, req = _correction_or_404(correction_id)
        decision = request.data.get("decision")
        if decision not in ("accept", "reject"):
            raise ValidationError({"decision": "Use accept or reject."})
        if not 0 <= index < len(corr.entries):
            raise Http404
        entries = [dict(e) for e in corr.entries]
        entries[index]["decision"] = decision
        corr.entries = entries
        corr.save(update_fields=["entries", "updated_at"])
        audit(request, f"exams.correction_{decision}", target=corr, summary=f"{corr.exam.class_group.short_label} {corr.subject.name} · entry {index + 1}")
        if all(e.get("decision") for e in entries):
            accepted = [e for e in entries if e["decision"] == "accept"]
            if accepted:
                corr.entries = accepted
                corr.save(update_fields=["entries", "updated_at"])
                rejected = len(entries) - len(accepted)
                decide(req, "approve", request.user, note=f"{rejected} change{'s' if rejected != 1 else ''} rejected in moderation" if rejected else "")
            else:
                decide(req, "decline", request.user, note="Every change was rejected in moderation.")
        return Response(moderation(corr.exam.name))


class CorrectionsApproveAllView(SchoolAPIView):
    """Approve every pending marks correction on the exam."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        from apps.approvals.services import decide

        name = _series_name(request)
        pending = _pending_corrections(name)
        if not pending:
            raise ValidationError({"series": "No corrections are waiting."})
        with transaction.atomic():
            for corr, req in pending:
                corr.entries = [{k: v for k, v in e.items() if k != "decision"} for e in corr.entries]
                corr.save(update_fields=["entries", "updated_at"])
                decide(req, "approve", request.user)
        audit(request, "exams.corrections_approved", summary=f"{name} · {len(pending)} corrections")
        return Response(moderation(name))


class ModerateSheetView(SchoolAPIView):
    """Clear a mark sheet held for moderation: it goes back to submitted, ready to publish."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, sheet_id):
        sheet = MarkSheet.objects.filter(id=sheet_id, status=MarkSheet.Status.REVIEW).select_related("exam__class_group", "subject").first()
        if sheet is None:
            raise Http404
        sheet.status = MarkSheet.Status.SUBMITTED
        sheet.moderated_at, sheet.moderated_by = timezone.now(), request.user
        sheet.save(update_fields=["status", "moderated_at", "moderated_by", "updated_at"])
        audit(request, "exams.moderated", target=sheet, summary=f"{sheet.exam.name} · {sheet.exam.class_group.short_label} {sheet.subject.name}")
        return Response(moderation(sheet.exam.name))


class AssignInvigilatorsView(SchoolAPIView):
    """Fill every paper of the next exam up to the invigilators-per-room rule: teachers free at that time and not on
    leave that day, fewest duties first."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        from apps.staff.models import StaffLeave

        school = request.school
        plan = upcoming(school)
        if plan is None:
            raise ValidationError({"exam": "No exam is coming up."})
        per_room = plan["per_room"]
        papers = list(ExamPaper.objects.filter(exam__name=plan["name"]).select_related("exam__class_group").order_by("date", "starts_at"))
        staff = sorted(teachers().values(), key=lambda u: u.full_name)
        duties = Counter(Invigilation.objects.filter(paper__in=papers).values_list("teacher_id", flat=True))
        busy = defaultdict(set)
        have = defaultdict(set)
        for inv in Invigilation.objects.filter(paper__in=papers).select_related("paper"):
            busy[inv.paper.date].add((inv.teacher_id, inv.paper.starts_at, inv.paper.ends_at))
            have[inv.paper_id].add(inv.teacher_id)
        leave = defaultdict(set)
        for lv in StaffLeave.objects.filter(status=StaffLeave.Status.APPROVED, to_date__gte=papers[0].date if papers else school_today(school)):
            d = lv.from_date
            while d <= lv.to_date:
                leave[d].add(lv.user_id)
                d += timedelta(days=1)
        added = []
        with transaction.atomic():
            for p in papers:
                while len(have[p.id]) < per_room:
                    free = [
                        u
                        for u in staff
                        if u.id not in leave[p.date]
                        and u.id not in have[p.id]
                        and not any(t == u.id and s < p.ends_at and p.starts_at < e for t, s, e in busy[p.date])
                    ]
                    if not free:
                        break
                    u = min(free, key=lambda u: (duties[u.id], u.full_name))
                    added.append(Invigilation(school=school, paper=p, teacher=u, assigned_by=request.user))
                    have[p.id].add(u.id)
                    busy[p.date].add((u.id, p.starts_at, p.ends_at))
                    duties[u.id] += 1
            Invigilation.objects.bulk_create(added)
        by_teacher = defaultdict(list)
        for inv in added:
            by_teacher[inv.teacher].append(inv.paper)
        for u, ps in by_teacher.items():
            days = ", ".join(sorted({f"{p.date:%a} {p.date.day} {p.date:%b}" for p in ps}))
            notify([u], school=school, category=Category.GENERAL, title=f"Invigilation: {plan['name']}", body=f"{days} · 9:00 AM. Check the roster for rooms."[:180], data={"type": "invigilation"})
        audit(request, "exams.invigilators", summary=f"{plan['name']} · {len(added)} duties for {len(by_teacher)} teachers")
        return Response({"added": len(added), "teachers": len(by_teacher), "upcoming": upcoming(school)})


class DatesheetView(SchoolAPIView):
    """The next exam's date sheet as CSV (every section's paper, room and invigilators)."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        plan = upcoming(request.school)
        if plan is None:
            raise Http404
        papers = ExamPaper.objects.filter(exam__name=plan["name"]).select_related("exam__class_group", "subject").prefetch_related("invigilations__teacher").order_by("date", "starts_at", "exam__class_group__grade")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Class", "Paper", "Starts", "Ends", "Room", "Invigilators"])
        for p in sorted(papers, key=lambda p: (p.date, p.starts_at, _grade_key(p.exam.class_group.grade), p.exam.class_group.section)):
            w.writerow([p.date.isoformat(), p.exam.class_group.short_label, p.subject.name, f"{p.starts_at:%H:%M}", f"{p.ends_at:%H:%M}", p.room, "; ".join(i.teacher.full_name for i in p.invigilations.all())])
        audit(request, "exams.datesheet_export", summary=plan["name"])
        response = HttpResponse(buf.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{plan["name"].lower().replace(" ", "-")}-datesheet.csv"'
        return response


class MessageTeachersView(SchoolAPIView):
    """POST {grade, subject_code, body}: a note to everyone teaching that subject in that grade."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        grade = str(request.data.get("grade") or "")
        code = str(request.data.get("subject_code") or "")
        body = str(request.data.get("body") or "").strip()
        if not body:
            raise ValidationError({"body": "Write the message."})
        users = {a.teacher for a in TeachingAssignment.objects.filter(class_group__grade=grade, subject__code=code).select_related("teacher", "subject")}
        if not users:
            raise ValidationError({"grade": "Nobody teaches that subject in that grade."})
        subject = TeachingAssignment.objects.filter(subject__code=code).select_related("subject").first().subject
        notify(list(users), school=request.school, category=Category.GENERAL, title=f"From {request.user.full_name}: Grade {grade} {subject.name}", body=body[:500], data={"type": "principal_note"})
        audit(request, "exams.message_teachers", summary=f"Grade {grade} {subject.name} · {len(users)} teachers")
        return Response({"sent_to": len(users)})


urlpatterns = [
    path("console/exams", ExamsView.as_view()),
    path("console/exams/publish", PublishResultsView.as_view()),
    path("console/exams/series", SeriesView.as_view()),
    path("console/exams/corrections/approve-all", CorrectionsApproveAllView.as_view()),
    path("console/exams/corrections/<str:correction_id>/entries/<int:index>", CorrectionEntryView.as_view()),
    path("console/exams/sheets/<uuid:sheet_id>/moderate", ModerateSheetView.as_view()),
    path("console/exams/invigilators", AssignInvigilatorsView.as_view()),
    path("console/exams/datesheet", DatesheetView.as_view()),
    path("console/exams/message", MessageTeachersView.as_view()),
]
