"""Console: the student directory, bulk actions, adding a student, and one student's profile."""

import csv
import io
import uuid
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q
from django.http import Http404, HttpResponse
from django.urls import path
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import AcademicYear, ClassGroup, Remark, RemarkAck, Student, StudentGuardian, Subject, TeachingAssignment
from apps.accounts.audit import audit
from apps.attendance.models import AttendanceException, AttendanceSession, LeaveApplication
from apps.core.api import SchoolAPIView
from apps.core.utils import normalize_phone, school_now, school_today, school_tz
from apps.fees.models import FeeInvoice, Payment
from apps.homework.models import Homework, HomeworkSubmission
from apps.messaging.models import Conversation, ConversationMember, Meeting, Message
from apps.principal.views import _grade_key, _pct
from apps.transport.models import BoardingEvent, Direction, StudentTransport, Trip

from .common import CONSOLE_ROLES, current_term

PAGE_SIZES = (12, 25, 50)
ABSENT = ("absent", "excused")
LATE = ("late", "half_day")
DUE_SOON_DAYS = 30


def masked(phone: str) -> str:
    """"+91 ••••• •4521": the country code and the last four digits, as the console shows parents' numbers."""
    return f"{phone[:3]} ••••• •{phone[-4:]}" if phone and len(phone) > 7 else "•••"


def year_start(today: date) -> date:
    year = AcademicYear.objects.filter(is_current=True).first()
    return year.starts_on if year else date(today.year if today.month >= 4 else today.year - 1, 4, 1)


def primary_guardians(student_ids) -> dict:
    out = {}
    for link in StudentGuardian.objects.filter(student_id__in=student_ids).select_related("user").order_by("-is_primary", "created_at"):
        out.setdefault(link.student_id, link)
    return out


def attendance_ytd(students, today: date) -> dict:
    """student id -> (days in, days marked, percent) since the academic year began; late counts as in."""
    start = year_start(today)
    groups = {s.class_group_id for s in students}
    marked = Counter(AttendanceSession.objects.filter(class_group_id__in=groups, date__gte=start, date__lte=today).values_list("class_group_id", flat=True))
    missed = Counter()
    for sid, status in AttendanceException.objects.filter(student__in=students, session__date__gte=start, session__date__lte=today).values_list("student_id", "status"):
        if status in ABSENT:
            missed[sid] += 1
        elif status == "half_day":
            missed[sid] += 0.5
    out = {}
    for s in students:
        total = marked.get(s.class_group_id, 0)
        present = total - missed.get(s.id, 0)
        out[s.id] = (present, total, _pct(present, total))
    return out


def ut_average(student_ids, exam_name: str = "Unit Test 2") -> dict:
    from apps.results.models import ExamMark

    acc = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for sid, marks, out_of in ExamMark.objects.filter(student_id__in=student_ids, exam__name=exam_name, is_absent=False).values_list("student_id", "marks", "max_marks"):
        acc[sid][0] += marks
        acc[sid][1] += out_of
    return {sid: round(float(got) * 100 / float(out_of)) for sid, (got, out_of) in acc.items() if out_of}


def fee_status(invoices, today: date) -> dict:
    """paid | overdue | due (with the date) | none, from a student's invoices."""
    unpaid = [i for i in invoices if i.balance > 0]
    overdue = [i for i in unpaid if i.due_date < today]
    if overdue:
        return {"status": "overdue", "due_on": min(i.due_date for i in overdue).isoformat(), "amount": str(sum(i.balance for i in overdue))}
    soon = [i for i in unpaid if i.due_date <= today + timedelta(days=DUE_SOON_DAYS)]
    if soon:
        return {"status": "due", "due_on": min(i.due_date for i in soon).isoformat(), "amount": str(sum(i.balance for i in soon))}
    if invoices:
        return {"status": "paid", "due_on": None, "amount": "0"}
    return {"status": "none", "due_on": None, "amount": "0"}


def overdue_ids(today: date) -> list:
    """Students with fees past due this academic year, by the Fees page's rules (instalment plans that are
    on track don't count), so the chip and the fee ledger agree."""
    from apps.core.tenant import current_school

    from .fees import overdue_families, resolve_period

    school = current_school()
    families = overdue_families(school, today, resolve_period(school, today, "year"))
    return [c["id"] for f in families for c in f["children"]]


def term_start(school, today: date) -> date:
    term = current_term(school, today)
    return date.fromisoformat(term["starts_on"]) if term else year_start(today)


# ---------------------------------------------------------------------------------------------- directory


def directory_queryset(request, params):
    school = request.school
    today = school_today(school)
    qs = Student.objects.filter(is_active=True).select_related("class_group")
    chip = params.get("chip") or "all"
    if chip == "absent":
        qs = qs.filter(attendance_exceptions__session__date=today, attendance_exceptions__status__in=ABSENT)
    elif chip == "late":
        qs = qs.filter(attendance_exceptions__session__date=today, attendance_exceptions__status__in=LATE)
    elif chip == "overdue":
        qs = qs.filter(id__in=overdue_ids(today))
    elif chip == "new":
        qs = qs.filter(created_at__date__gte=term_start(school, today))
    if params.get("class"):
        qs = qs.filter(class_group_id=params["class"])
    if params.get("grade"):
        qs = qs.filter(class_group__grade=params["grade"])
    if params.get("section"):
        qs = qs.filter(class_group__section=params["section"])
    transport = params.get("transport")
    if transport == "none":
        qs = qs.exclude(transport__is_active=True)
    elif transport:
        qs = qs.filter(transport__route_id=transport, transport__is_active=True)
    fee = params.get("fee")
    if fee == "overdue":
        qs = qs.filter(id__in=overdue_ids(today))
    elif fee == "due":
        soon = FeeInvoice.objects.filter(due_date__gte=today, due_date__lte=today + timedelta(days=DUE_SOON_DAYS), paid_amount__lt=F("amount")).values("student_id")
        qs = qs.filter(id__in=soon).exclude(id__in=overdue_ids(today))
    elif fee == "paid":
        unpaid = FeeInvoice.objects.filter(due_date__lte=today + timedelta(days=DUE_SOON_DAYS), paid_amount__lt=F("amount")).values("student_id")
        qs = qs.exclude(id__in=unpaid).filter(invoices__isnull=False)
    q = (params.get("q") or "").strip()
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        match = Q(full_name__icontains=q) | Q(admission_no__icontains=q)
        if len(digits) >= 4:
            match |= Q(guardian_links__user__phone__contains=digits)
        qs = qs.filter(match)
    order = params.get("sort") or "name"
    fields = {
        "name": ["full_name"],
        "-name": ["-full_name"],
        "class": ["class_group__grade", "class_group__section", "roll_no"],
        "roll": ["roll_no", "full_name"],
    }.get(order, ["full_name"])
    if order == "class":
        # Grades sort Nursery → 12, not as text.
        ids = list(qs.values_list("id", "class_group__grade", "class_group__section", "roll_no").distinct())
        ids.sort(key=lambda r: (_grade_key(r[1]), r[2], r[3]))
        return qs.distinct(), [r[0] for r in ids]
    return qs.distinct().order_by(*fields, "id"), None


def row_payloads(students, today: date, new_since: date) -> list[dict]:
    ids = [s.id for s in students]
    att = attendance_ytd(students, today)
    ut2 = ut_average(ids)
    invoices = defaultdict(list)
    for inv in FeeInvoice.objects.filter(student_id__in=ids):
        invoices[inv.student_id].append(inv)
    rides = {t.student_id: t for t in StudentTransport.objects.filter(student_id__in=ids, is_active=True).select_related("route", "pickup_stop")}
    parents = primary_guardians(ids)
    out = []
    for s in students:
        link = parents.get(s.id)
        ride = rides.get(s.id)
        out.append(
            {
                "id": str(s.id),
                "name": s.full_name,
                "initials": s.initials,
                "admission_no": s.admission_no,
                "is_new": s.created_at.date() >= new_since,
                "class": {"id": str(s.class_group_id), "label": s.class_group.short_label},
                "roll_no": s.roll_no,
                "attendance": att[s.id][2],
                "ut2": ut2.get(s.id),
                "fee": fee_status(invoices.get(s.id, []), today),
                "transport": {"route_id": str(ride.route_id), "route": ride.route.name, "stop": ride.pickup_stop.name} if ride else None,
                "parent": {"name": link.user.full_name, "relationship": link.relationship, "phone_masked": masked(link.user.phone)} if link else None,
            }
        )
    return out


class StudentDirectoryView(SchoolAPIView):
    """GET the directory (filters, chips, a page of rows). POST adds a student with a guardian."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        from apps.transport.models import Route

        school = request.school
        today = school_today(school)
        params = request.query_params
        try:
            size = int(params.get("page_size") or 12)
            page = max(1, int(params.get("page") or 1))
        except ValueError as exc:
            raise ValidationError({"page": "Use a whole number."}) from exc
        if size not in PAGE_SIZES:
            raise ValidationError({"page_size": f"Use one of {', '.join(map(str, PAGE_SIZES))}."})
        qs, ordered_ids = directory_queryset(request, params)
        total = len(ordered_ids) if ordered_ids is not None else qs.count()
        pages = max(1, -(-total // size))
        page = min(page, pages)
        if ordered_ids is not None:
            window = ordered_ids[(page - 1) * size : page * size]
            by_id = {s.id: s for s in Student.objects.filter(id__in=window).select_related("class_group")}
            students = [by_id[i] for i in window]
        else:
            students = list(qs[(page - 1) * size : page * size])

        active = Student.objects.filter(is_active=True)
        new_since = term_start(school, today)
        counts = {
            "all": active.count(),
            "absent": active.filter(attendance_exceptions__session__date=today, attendance_exceptions__status__in=ABSENT).distinct().count(),
            "late": active.filter(attendance_exceptions__session__date=today, attendance_exceptions__status__in=LATE).distinct().count(),
            "overdue": active.filter(id__in=overdue_ids(today)).count(),
            "new": active.filter(created_at__date__gte=new_since).count(),
        }
        groups = [g for g in ClassGroup.objects.filter(academic_year__is_current=True) if active.filter(class_group=g).exists()]
        grades = sorted({g.grade for g in groups}, key=_grade_key)
        year = AcademicYear.objects.filter(is_current=True).first()
        return Response(
            {
                "counts": counts,
                "summary": {
                    "enrolled": counts["all"],
                    "sections": len(groups),
                    "first_grade": grades[0] if grades else None,
                    "last_grade": grades[-1] if grades else None,
                    "academic_year": year.name if year else None,
                },
                "facets": {
                    "grades": grades,
                    "sections": sorted({g.section for g in groups}),
                    "classes": [{"id": str(g.id), "label": g.short_label, "grade": g.grade, "section": g.section} for g in sorted(groups, key=lambda g: (_grade_key(g.grade), g.section))],
                    "routes": [{"id": str(r.id), "label": r.name} for r in Route.objects.filter(is_active=True).order_by("code")],
                },
                "page": page,
                "page_size": size,
                "pages": pages,
                "total": total,
                "items": row_payloads(students, today, new_since),
            }
        )

    def post(self, request):
        data = request.data
        errors = {}
        name = " ".join(str(data.get("full_name") or "").split())
        if len(name) < 2:
            errors["full_name"] = "Enter the child's full name."
        group = ClassGroup.objects.filter(id=data.get("class_id")).first() if _is_uuid(data.get("class_id")) else None
        if group is None:
            errors["class_id"] = "Choose a class."
        dob = None
        if data.get("date_of_birth"):
            try:
                dob = date.fromisoformat(str(data["date_of_birth"]))
            except ValueError:
                errors["date_of_birth"] = "Use a date like 2015-03-14."
            else:
                if dob >= school_today(request.school):
                    errors["date_of_birth"] = "The date of birth must be in the past."
        gender = str(data.get("gender") or "")
        if gender not in ("", "male", "female", "other"):
            errors["gender"] = "Choose male, female or other."
        guardian_name = " ".join(str(data.get("guardian_name") or "").split())
        if len(guardian_name) < 2:
            errors["guardian_name"] = "Enter the parent's or guardian's name."
        try:
            phone = normalize_phone(data.get("guardian_phone"))
        except ValueError as exc:
            errors["guardian_phone"] = str(exc)
            phone = None
        relationship = str(data.get("relationship") or "parent")
        if relationship not in ("father", "mother", "guardian", "parent"):
            errors["relationship"] = "Choose father, mother or guardian."
        if errors:
            raise ValidationError(errors)
        from apps.admissions.services import link_guardian, next_admission_no

        today = school_today(request.school)
        with transaction.atomic():
            roll = (Student.objects.filter(class_group=group).order_by("-roll_no").values_list("roll_no", flat=True).first() or 0) + 1
            student = Student.objects.create(
                full_name=name, admission_no=next_admission_no(today.year), roll_no=roll, class_group=group, date_of_birth=dob, gender=gender
            )
            guardian = link_guardian(student, request.school, guardian_name, phone, relationship)
            StudentGuardian.objects.filter(student=student, user=guardian).update(is_primary=True, relationship=relationship)
        audit(request, "students.create", target=student, summary=f"{student.full_name} · {group.short_label} · {student.admission_no}")
        return Response({"id": str(student.id), "admission_no": student.admission_no, "roll_no": student.roll_no}, status=201)


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _selected(request) -> list:
    raw = request.data.get("student_ids") if request.method == "POST" else request.query_params.get("ids", "")
    ids = raw if isinstance(raw, list) else [i for i in str(raw or "").split(",") if i]
    if not ids or not all(_is_uuid(i) for i in ids):
        raise ValidationError({"student_ids": "Choose at least one student."})
    if len(ids) > 500:
        raise ValidationError({"student_ids": "Choose up to 500 students at a time."})
    students = list(Student.objects.filter(id__in=ids, is_active=True).select_related("class_group"))
    if len(students) != len(set(ids)):
        raise Http404
    return students


class StudentExportView(SchoolAPIView):
    """A CSV of the chosen students (``?ids=``) or of everyone matching the filters. Every export is audited."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        today = school_today(school)
        if request.query_params.get("ids"):
            students = sorted(_selected(request), key=lambda s: s.full_name)
        else:
            qs, ordered = directory_queryset(request, request.query_params)
            students = list(qs) if ordered is None else [s for s in qs]
        rows = row_payloads(students, today, term_start(school, today))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Admission no", "Name", "Class", "Roll", "Attendance YTD %", "UT2 avg", "Fee status", "Transport", "Parent", "Parent phone"])
        for r in rows:
            fee = r["fee"]["status"] + (f" ({r['fee']['due_on']})" if r["fee"]["due_on"] else "")
            writer.writerow([
                r["admission_no"], r["name"], r["class"]["label"], r["roll_no"], r["attendance"] if r["attendance"] is not None else "",
                r["ut2"] if r["ut2"] is not None else "", fee, r["transport"]["route"] if r["transport"] else "Walks / pick-up",
                r["parent"]["name"] if r["parent"] else "", r["parent"]["phone_masked"] if r["parent"] else "",
            ])
        audit(request, "students.export", target=("student", ",".join(r["id"] for r in rows[:20])), summary=f"Exported {len(rows)} student{'s' if len(rows) != 1 else ''}", detail={"count": len(rows), "filters": {k: v for k, v in request.query_params.items() if k != "ids"}})
        response = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="students-{today.isoformat()}.csv"'
        return response


def _conversation_with_family(student, guardian, principal) -> Conversation:
    from apps.messaging.services import get_or_create_direct

    return get_or_create_direct(guardian, principal, student, staff_label="Principal", family_label=f"Parent of {student.first_name}")


class StudentBulkMessageView(SchoolAPIView):
    """Message the parents of the chosen students: one chat per family, from the principal."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        from apps.messaging.services import send_message

        students = _selected(request)
        body = str(request.data.get("body") or "").strip()
        if not body:
            raise ValidationError({"body": "Type a message."})
        parents = primary_guardians([s.id for s in students])
        sent = 0
        for s in students:
            link = parents.get(s.id)
            if not link:
                continue
            conversation = _conversation_with_family(s, link.user, request.user)
            send_message(conversation, request.user, body, client_id=f"console-bulk-{uuid.uuid4()}")
            sent += 1
        audit(request, "students.message", target=("student", ",".join(str(s.id) for s in students[:20])), summary=f"Messaged {sent} famil{'y' if sent == 1 else 'ies'}", detail={"students": len(students), "sent": sent})
        return Response({"sent": sent, "skipped": len(students) - sent})


# ---------------------------------------------------------------------------------------------- profile


def _student(student_id) -> Student:
    if not _is_uuid(student_id):
        raise Http404
    student = Student.objects.filter(id=student_id).select_related("class_group", "class_group__class_teacher").first()
    if student is None:
        raise Http404
    return student


def _age(dob: date | None, today: date) -> int | None:
    if not dob:
        return None
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _subject_of(user, group=None) -> str | None:
    qs = TeachingAssignment.objects.filter(teacher=user).select_related("subject")
    if group is not None:
        mine = qs.filter(class_group=group).first()
        if mine:
            return mine.subject.name
    first = qs.first()
    return first.subject.name if first else None


def _attendance(student, today: date, month: date) -> dict:
    start = year_start(today)
    group = student.class_group
    sessions = set(AttendanceSession.objects.filter(class_group=group, date__gte=start, date__lte=today).values_list("date", flat=True))
    exc = {
        e.session.date: e
        for e in AttendanceException.objects.filter(student=student, session__date__gte=start, session__date__lte=today).select_related("session")
    }
    leaves = list(LeaveApplication.objects.filter(student=student, from_date__lte=today).select_related("decided_by"))

    def state(day):
        if day.weekday() == 6:
            return "holiday"
        if day > today:
            return "upcoming"
        if day not in sessions:
            return "holiday"
        e = exc.get(day)
        if e is None:
            return "present"
        return "absent" if e.status in ABSENT else "late"

    first = month.replace(day=1)
    days = [first + timedelta(days=i) for i in range(monthrange(first.year, first.month)[1])]
    cal = [{"date": d.isoformat(), "state": state(d)} for d in days]
    counted = [c for c in cal if c["state"] in ("present", "late", "absent")]
    tally = Counter(c["state"] for c in counted)
    ytd_absent = sum(1 for d, e in exc.items() if e.status in ABSENT) + sum(0.5 for e in exc.values() if e.status == "half_day")
    present = len(sessions) - ytd_absent

    def leave_for(day):
        return next((lv for lv in leaves if lv.from_date <= day <= lv.to_date), None)

    log = []
    for day in sorted(exc, reverse=True):
        e = exc[day]
        lv = leave_for(day)
        log.append(
            {
                "date": day.isoformat(),
                "status": "absent" if e.status in ABSENT else "late",
                "raw_status": e.status,
                "note": e.note,
                "leave": {"reason": lv.reason, "status": lv.status, "decided_by": lv.decided_by.full_name if lv.decided_by else None} if lv else None,
            }
        )
    # A month-by-month summary for the Attendance tab.
    months = []
    cursor = start.replace(day=1)
    while cursor <= today:
        marked = [d for d in sessions if d.year == cursor.year and d.month == cursor.month]
        missed = [d for d in marked if d in exc and exc[d].status in ABSENT]
        late = [d for d in marked if d in exc and exc[d].status in LATE]
        months.append({"month": cursor.isoformat()[:7], "days": len(marked), "absent": len(missed), "late": len(late), "percent": _pct(len(marked) - len(missed), len(marked))})
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return {
        "month": first.isoformat()[:7],
        "calendar": cal,
        "month_stats": {"percent": _pct(tally["present"] + tally["late"], len(counted)), "on_time": tally["present"], "late": tally["late"], "absent": tally["absent"]},
        "ytd": {"present": present, "days": len(sessions), "percent": _pct(present, len(sessions)), "target": 90},
        "log": log,
        "months": months,
    }


def _today_status(student, today: date, tz) -> dict:
    session = AttendanceSession.objects.filter(class_group=student.class_group, date=today).first()
    exc = AttendanceException.objects.filter(student=student, session__date=today).first()
    arrived = (
        BoardingEvent.objects.filter(student=student, kind="dropped", trip__service_date=today, trip__direction=Direction.PICKUP).order_by("at").first()
    )
    if exc and exc.status in ABSENT:
        return {"state": "absent", "since": None, "note": exc.note}
    since = arrived.at.astimezone(tz).strftime("%H:%M") if arrived else None
    if exc and exc.status in LATE:
        return {"state": "late", "since": since, "note": exc.note}
    if session:
        return {"state": "in_school", "since": since, "note": ""}
    return {"state": "not_marked", "since": None, "note": ""}


def _academics(student, school) -> dict:
    from apps.results.models import Exam
    from apps.results.services import student_results

    results = student_results(student)
    exams = list(reversed(results["exams"]))  # oldest first
    latest = exams[-1] if exams else None
    before = exams[-2] if len(exams) > 1 else None
    order = {code: i for i, code in enumerate(Subject.objects.order_by("created_at").values_list("code", flat=True))}
    subjects = []
    if latest:
        prev = {s["code"]: s for s in (before["subjects"] if before else [])}
        for s in sorted(latest["subjects"], key=lambda s: order.get(s["code"], 99)):
            p = prev.get(s["code"])
            subjects.append(
                {
                    "code": s["code"],
                    "subject": s["subject"],
                    "teacher": s["teacher"],
                    "previous": p["percent"] if p else None,
                    "latest": s["percent"],
                    "grade": s["grade"],
                    "class_average": s["class_average"],
                    "change": round(s["percent"] - p["percent"], 1) if p else None,
                }
            )
    watch = None
    if subjects:
        lowest = min(subjects, key=lambda s: s["latest"])
        gains = [s for s in subjects if s["change"] is not None]
        best = max(gains, key=lambda s: s["change"]) if gains else None
        watch = {"lowest": lowest["subject"], "lowest_percent": lowest["latest"], "biggest_gain": best["subject"] if best else None, "gain": best["change"] if best else None}
    upcoming = Exam.objects.filter(class_group=student.class_group, is_published=False, held_on__gte=school_today(school)).order_by("held_on").first()
    return {
        "exams": [{"id": e["id"], "name": e["name"], "percent": e["percent"], "grade": e["grade"], "class_average": e["class_average"], "published_on": e["published_on"], "note": e["note"]} for e in exams],
        "previous_name": before["name"] if before else None,
        "latest_name": latest["name"] if latest else None,
        "latest_published_on": latest["published_on"] if latest else None,
        "latest_percent": latest["percent"] if latest else None,
        "latest_grade": latest["grade"] if latest else None,
        "latest_class_average": latest["class_average"] if latest else None,
        "previous_percent": before["percent"] if before else None,
        "subjects": subjects,
        "watch": watch,
        "next_exam": {"name": upcoming.name, "held_on": upcoming.held_on.isoformat()} if upcoming else None,
        "report_exam_id": latest["id"] if latest else None,
    }


def _remarks(student, guardians) -> list[dict]:
    guardian_ids = {g.user_id for g in guardians}
    group = student.class_group
    items = []
    for r in Remark.objects.filter(student=student).select_related("author", "homework").order_by("-created_at"):
        ack = RemarkAck.objects.filter(remark=r, user_id__in=guardian_ids).order_by("created_at").first()
        subject = _subject_of(r.author, group)
        items.append(
            {
                "id": str(r.id),
                "author": r.author.full_name,
                "author_role": "class_teacher" if group.class_teacher_id == r.author_id else "teacher",
                "subject": subject,
                "tone": r.tone,
                "body": r.body,
                "visibility": r.visibility,
                "created_at": r.created_at.isoformat(),
                "requires_ack": r.requires_ack,
                "seen_at": ack.created_at.isoformat() if ack else None,
            }
        )
    return items


def _fees(student, today: date, siblings) -> dict:
    start = year_start(today)
    invoices = list(FeeInvoice.objects.filter(student=student).order_by("due_date"))
    year = [i for i in invoices if i.due_date >= start]
    payments = list(Payment.objects.filter(invoice__student=student, status=Payment.Status.SUCCEEDED).select_related("invoice").order_by("-paid_at"))
    unpaid = [i for i in invoices if i.balance > 0]
    nxt = unpaid[0] if unpaid else None
    categories = sorted({i.get_category_display().lower() for i in year})
    family = [student] + list(siblings)
    family_due = []
    if nxt:
        for s in family:
            owed = sum((i.balance for i in FeeInvoice.objects.filter(student=s, due_date__lte=nxt.due_date) if i.balance > 0), Decimal(0))
            if owed:
                family_due.append({"id": str(s.id), "name": s.first_name, "class": s.class_group.short_label, "amount": str(owed)})
    last = payments[0] if payments else None
    return {
        "annual": str(sum((i.amount for i in year), Decimal(0))),
        "categories": categories,
        "paid": str(sum((i.paid_amount for i in year), Decimal(0))),
        "outstanding": str(sum((i.balance for i in year), Decimal(0))),
        "receipts": len([p for p in payments if p.invoice.due_date >= start]),
        "status": fee_status(invoices, today),
        "next": {"id": str(nxt.id), "title": nxt.title, "amount": str(nxt.balance), "due_date": nxt.due_date.isoformat(), "days": (nxt.due_date - today).days} if nxt else None,
        "upcoming": [{"id": str(i.id), "title": i.title, "amount": str(i.balance), "due_date": i.due_date.isoformat()} for i in unpaid[1:4]],
        "family_due": {"on": nxt.due_date.isoformat(), "total": str(sum(Decimal(f["amount"]) for f in family_due)), "children": family_due} if nxt and len(family_due) > 1 else None,
        "last_payment": {"amount": str(last.amount), "paid_at": last.paid_at.isoformat() if last.paid_at else None, "receipt_no": last.receipt_no} if last else None,
        "invoices": [
            {"id": str(i.id), "title": i.title, "category": i.category, "amount": str(i.amount), "paid": str(i.paid_amount), "balance": str(i.balance), "due_date": i.due_date.isoformat(), "status": i.status_on(today)}
            for i in invoices
        ],
        "payments": [
            {"id": str(p.id), "title": p.invoice.title, "amount": str(p.amount), "paid_at": p.paid_at.isoformat() if p.paid_at else None, "receipt_no": p.receipt_no, "method": p.method}
            for p in payments
        ],
    }


def _homework(student, school, today: date) -> dict:
    start = term_start(school, today)
    items = list(Homework.objects.filter(class_group=student.class_group, assigned_on__gte=start).select_related("subject", "assigned_by").order_by("due_date"))
    subs = {s.homework_id: s for s in HomeworkSubmission.objects.filter(student=student, homework__in=items)}
    due = [h for h in items if h.due_date < today]
    on_time = [h for h in due if h.id in subs and subs[h.id].submitted_at.date() <= h.due_date]
    late = [h for h in due if h.id in subs and subs[h.id].submitted_at.date() > h.due_date]
    missing = [h for h in due if h.id not in subs]
    flagged = Remark.objects.filter(student=student, homework__in=items, tone="concern").select_related("homework", "author", "homework__subject").order_by("-created_at").first()
    open_items = [h for h in items if h.due_date >= today and h.id not in subs]

    def item(h):
        s = subs.get(h.id)
        return {
            "id": str(h.id),
            "subject": h.subject.name,
            "code": h.subject.code,
            "title": h.title,
            "assigned_on": h.assigned_on.isoformat(),
            "due_date": h.due_date.isoformat(),
            "teacher": h.assigned_by.full_name if h.assigned_by else None,
            "status": "open" if h.due_date >= today and not s else "missing" if not s else "late" if s.submitted_at.date() > h.due_date else s.status,
            "grade": s.grade if s else "",
            "remark": s.teacher_remark if s else "",
        }

    return {
        "assigned": len(due),
        "on_time": len(on_time),
        "late": len(late),
        "incomplete": len(missing) + (1 if flagged and flagged.homework_id not in {h.id for h in missing} else 0),
        "percent": _pct(len(on_time), len(due)),
        "open": [item(h) for h in open_items],
        "flag": {
            "title": flagged.homework.title,
            "subject": flagged.homework.subject.name,
            "by": flagged.author.full_name,
            "body": flagged.body,
            "due_date": flagged.homework.due_date.isoformat(),
        }
        if flagged
        else None,
        "all": [item(h) for h in reversed(items)],
    }


def _interactions(student, guardians, today, tz) -> list[dict]:
    guardian_ids = [g.user_id for g in guardians]
    out = []
    conversations = list(Conversation.objects.filter(student=student, members__user_id__in=guardian_ids).distinct())
    for c in conversations:
        staff = ConversationMember.objects.filter(conversation=c, side="staff").select_related("user").first()
        messages = list(Message.objects.filter(conversation=c, deleted_at__isnull=True).order_by("created_at"))
        if messages:
            last = messages[-1]
            replied = any(m.sender_id not in guardian_ids for m in messages) and any(m.sender_id in guardian_ids for m in messages)
            first_family = next((m for m in messages if m.sender_id in guardian_ids), None)
            first_reply = next((m for m in messages if first_family and m.sender_id not in guardian_ids and m.created_at > first_family.created_at), None)
            reply_minutes = int((first_reply.created_at - first_family.created_at).total_seconds() // 60) if first_reply else None
            out.append(
                {
                    "kind": "thread",
                    "at": last.created_at.isoformat(),
                    "title": "Message thread",
                    "with": staff.user.full_name if staff else None,
                    "detail": last.body[:120] or (last.attachment_name or ""),
                    "count": len(messages),
                    "status": "replied" if replied else "open",
                    "reply_minutes": reply_minutes,
                    "conversation_id": str(c.id),
                    "upcoming": False,
                }
            )
        for m in Meeting.objects.filter(conversation=c).order_by("starts_at"):
            upcoming = m.starts_at.astimezone(tz).date() >= today
            out.append(
                {
                    "kind": "meeting",
                    "at": m.starts_at.isoformat(),
                    "title": m.title,
                    "with": staff.user.full_name if staff else None,
                    "detail": m.location,
                    "status": "booked" if upcoming and m.status != "cancelled" else ("cancelled" if m.status == "cancelled" else "attended"),
                    "conversation_id": str(c.id),
                    "upcoming": upcoming,
                    "ends_at": m.ends_at.isoformat(),
                }
            )
    for ack in RemarkAck.objects.filter(remark__student=student, user_id__in=guardian_ids).select_related("remark", "remark__author", "remark__homework"):
        out.append(
            {
                "kind": "ack",
                "at": ack.created_at.isoformat(),
                "title": "Remark acknowledged",
                "with": ack.remark.author.full_name,
                "detail": ack.remark.homework.title if ack.remark.homework else ack.remark.body[:90],
                "status": "seen",
                "upcoming": False,
            }
        )
    for lv in LeaveApplication.objects.filter(student=student).select_related("decided_by"):
        out.append(
            {
                "kind": "leave",
                "at": datetime.combine(lv.from_date, datetime.min.time(), tzinfo=tz).isoformat(),
                "title": "Leave note",
                "with": lv.decided_by.full_name if lv.decided_by else None,
                "detail": lv.reason,
                "status": {"approved": "resolved", "pending": "open"}.get(lv.status, lv.status),
                "days": (lv.to_date - lv.from_date).days + 1,
                "upcoming": False,
            }
        )
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


def _transport(student, school, today: date, tz) -> dict | None:
    from apps.transport import services

    ride = StudentTransport.objects.filter(student=student, is_active=True).select_related("route", "route__vehicle", "route__driver", "route__attendant", "pickup_stop", "drop_stop").first()
    if not ride:
        return None
    route = ride.route
    stops = route.stops.filter(is_school=False).count()

    def offset(start, minutes):
        total = start.hour * 60 + start.minute + minutes
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"

    out = {
        "route": {"id": str(route.id), "code": route.code, "name": route.name},
        "vehicle": route.vehicle.registration_no if route.vehicle else None,
        "stop": ride.pickup_stop.name,
        "stop_no": ride.pickup_stop.sequence,
        "stops": stops,
        "pickup_at": offset(route.pickup_start, ride.pickup_stop.pickup_offset_min),
        "drop_leaves": route.drop_start.strftime("%H:%M"),
        "drop_eta": offset(route.drop_start, ride.drop_stop.drop_offset_min),
        "boarded_at": None,
        "arrived_at": None,
        "drop": None,
        "driver": {"name": route.driver.full_name, "initials": route.driver.initials, "phone": route.driver.phone} if route.driver else None,
        "attendant": {"name": route.attendant.full_name, "initials": route.attendant.initials} if route.attendant else None,
        "delay_minutes": None,
    }
    for e in BoardingEvent.objects.filter(student=student, trip__service_date=today, trip__direction=Direction.PICKUP):
        key = "boarded_at" if e.kind == "boarded" else "arrived_at"
        out[key] = e.at.astimezone(tz).strftime("%H:%M")
    trip = Trip.objects.filter(route=route, service_date=today, direction=Direction.DROP).first()
    if trip:
        live = None
        if trip.status == Trip.Status.ACTIVE:
            live = services.live_state(trip)
            mine = next((s for s in live["stops"] if s["id"] == str(ride.drop_stop_id)), None)
            if mine and mine.get("eta_seconds") is not None:
                eta = school_now(school) + timedelta(seconds=mine["eta_seconds"])
                scheduled = datetime.combine(today, datetime.strptime(out["drop_eta"], "%H:%M").time(), tzinfo=tz)
                delay = int((eta - scheduled).total_seconds() // 60)
                # A run still "live" hours after its slot is a trip nobody closed, not a real delay.
                if delay <= 120:
                    out["delay_minutes"] = max(0, delay)
                    out["drop"] = {"status": "live", "eta": eta.astimezone(tz).strftime("%H:%M")}
        if out["drop"] is None:
            out["drop"] = {"status": trip.status, "eta": None}
    return out


def _documents(student) -> list[dict]:
    from apps.documents.views import document_payload, visible_documents
    from apps.results.services import student_results

    items = [
        {"id": f"exam-{e['id']}", "kind": "report_card", "title": f"{e['name']} · report card", "subtitle": f"{e['percent']:.0f}% · Grade {e['grade']}", "date": e["published_on"], "exam_id": e["id"]}
        for e in student_results(student)["exams"]
    ]
    items += [document_payload(d) for d in visible_documents(student).order_by("-issued_on", "-created_at")]
    return items


class StudentProfileView(SchoolAPIView):
    """Everything the profile's header and eight tabs show, for one student. ``?month=YYYY-MM`` moves the calendar."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, student_id):
        school = request.school
        student = _student(student_id)
        today = school_today(school)
        tz = school_tz(school)
        month = today
        if request.query_params.get("month"):
            try:
                month = date.fromisoformat(f"{request.query_params['month']}-01")
            except ValueError as exc:
                raise ValidationError({"month": "Use YYYY-MM."}) from exc
        group = student.class_group
        guardians = list(StudentGuardian.objects.filter(student=student).select_related("user").order_by("-is_primary", "created_at"))
        primary = guardians[0] if guardians else None
        siblings = list(
            Student.objects.filter(is_active=True, guardian_links__user_id__in=[g.user_id for g in guardians])
            .exclude(id=student.id)
            .select_related("class_group", "class_group__class_teacher")
            .distinct()
        )
        teacher = group.class_teacher
        profile = None
        if teacher:
            from apps.staff.models import StaffProfile

            profile = StaffProfile.objects.filter(user=teacher).first()
        academics = _academics(student, school)
        remarks = _remarks(student, guardians)
        homework = _homework(student, school, today)
        fees = _fees(student, today, siblings)
        documents = _documents(student)
        transport = _transport(student, school, today, tz)
        return Response(
            {
                "id": str(student.id),
                "name": student.full_name,
                "initials": student.initials,
                "is_active": student.is_active,
                "house": student.house,
                "academic_year": group.academic_year.name,
                "class": {"id": str(group.id), "label": group.short_label},
                "roll_no": student.roll_no,
                "admission_no": student.admission_no,
                "date_of_birth": student.date_of_birth.isoformat() if student.date_of_birth else None,
                "age": _age(student.date_of_birth, today),
                "gender": student.gender,
                "today": _today_status(student, today, tz),
                "class_teacher": {"name": teacher.full_name, "subject": _subject_of(teacher, group), "employee_id": profile.employee_id if profile else None} if teacher else None,
                "guardians": [
                    {"id": str(g.user_id), "name": g.user.full_name, "relationship": g.relationship, "is_primary": g.is_primary, "phone": g.user.phone, "phone_masked": masked(g.user.phone)}
                    for g in guardians
                ],
                "siblings": [
                    {"id": str(s.id), "name": s.full_name, "class": s.class_group.short_label, "roll_no": s.roll_no, "class_teacher": s.class_group.class_teacher.full_name if s.class_group.class_teacher else None}
                    for s in siblings
                ],
                "counts": {"homework": len(homework["open"]), "remarks": len(remarks), "documents": len(documents)},
                "attendance": _attendance(student, today, month),
                "academics": academics,
                "remarks": remarks,
                "fees": fees,
                "homework": homework,
                "interactions": _interactions(student, guardians, today, tz),
                "transport": transport,
                "documents": documents,
                "primary_guardian_id": str(primary.user_id) if primary else None,
            }
        )


class StudentReportCardView(SchoolAPIView):
    """The report-card PDF (the families' generator), audited. ``?exam=`` picks the exam; default is the latest."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, student_id):
        from apps.documents.models import DocumentDownload
        from apps.documents.pdf import report_card_pdf
        from apps.results.services import student_results

        student = _student(student_id)
        results = student_results(student)
        if not results["exams"]:
            raise ValidationError({"exam": "No results are published for this student yet."})
        exam_id = request.query_params.get("exam") or results["exams"][0]["id"]
        exam = next((e for e in results["exams"] if e["id"] == str(exam_id)), None)
        if exam is None:
            raise Http404
        pdf = report_card_pdf(results, exam["id"])
        DocumentDownload.objects.create(kind="report_card", ref=exam["id"], user=request.user, student=student)
        audit(request, "students.report_card", target=student, summary=f"Printed {exam['name']} report card · {student.full_name}")
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{student.full_name.replace(" ", "_")}_{exam["name"].replace(" ", "_")}.pdf"'
        return response


class StudentMessageView(SchoolAPIView):
    """Open (or reuse) the principal's chat with this child's parent; send ``body`` if given."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, student_id):
        from apps.messaging.services import send_message

        student = _student(student_id)
        links = primary_guardians([student.id])
        link = links.get(student.id)
        guardian_id = request.data.get("guardian_id")
        if guardian_id:
            link = StudentGuardian.objects.filter(student=student, user_id=guardian_id).select_related("user").first() if _is_uuid(guardian_id) else None
        if link is None:
            raise ValidationError({"guardian_id": "This student has no parent or guardian on record."})
        conversation = _conversation_with_family(student, link.user, request.user)
        body = str(request.data.get("body") or "").strip()
        message = None
        if body:
            message, _ = send_message(conversation, request.user, body, client_id=str(request.data.get("client_id") or f"console-{uuid.uuid4()}"))
        return Response({"conversation_id": str(conversation.id), "message_id": str(message.id) if message else None, "to": link.user.full_name}, status=201 if message else 200)


class StudentRemarkView(SchoolAPIView):
    """The principal adds a remark to the child's record (visible to the family unless marked staff-only)."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, student_id):
        student = _student(student_id)
        body = str(request.data.get("body") or "").strip()
        tone = str(request.data.get("tone") or "info")
        visibility = str(request.data.get("visibility") or Remark.Visibility.FAMILY)
        errors = {}
        if len(body) < 3:
            errors["body"] = "Write the remark."
        if len(body) > 1000:
            errors["body"] = "Keep remarks under 1,000 characters."
        if tone not in ("positive", "concern", "info"):
            errors["tone"] = "Choose praise, needs attention or general."
        if visibility not in Remark.Visibility.values:
            errors["visibility"] = "Choose who can see it."
        if errors:
            raise ValidationError(errors)
        remark = Remark.objects.create(student=student, author=request.user, body=body, tone=tone, visibility=visibility, requires_ack=tone == "concern")
        if visibility == Remark.Visibility.FAMILY:
            from apps.academics.access import guardians_of
            from apps.notifications.models import Category
            from apps.notifications.services import notify

            notify(list(guardians_of([student]).keys()), school=request.school, category=Category.GENERAL, title=f"A note about {student.first_name}", body=body[:180], data={"type": "remark", "remark_id": str(remark.id), "student_id": str(student.id)})
        return Response({"id": str(remark.id)}, status=201)


class StudentFeeReminderView(SchoolAPIView):
    """Remind the family about what's due next. Reminders are audited."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, student_id):
        from apps.academics.access import guardians_of
        from apps.notifications.models import Category
        from apps.notifications.services import notify

        student = _student(student_id)
        today = school_today(request.school)
        unpaid = [i for i in FeeInvoice.objects.filter(student=student).order_by("due_date") if i.balance > 0]
        if not unpaid:
            raise ValidationError({"fees": "Nothing is due for this student."})
        nxt = unpaid[0]
        families = guardians_of([student])
        if not families:
            raise ValidationError({"fees": "This student has no parent or guardian who receives alerts."})
        when = "was due" if nxt.due_date < today else "is due"
        notify(
            list(families.keys()),
            school=request.school,
            category=Category.FEES,
            title=f"Fee reminder · {student.first_name}",
            body=f"{nxt.title}: ₹{nxt.balance:,.0f} {when} on {nxt.due_date:%d %b}.",
            data={"type": "fees", "student_id": str(student.id), "invoice_id": str(nxt.id)},
        )
        audit(request, "fees.remind", target=student, module="fees", summary=f"Fee reminder · {student.full_name} · {nxt.title} ₹{nxt.balance:,.0f}")
        return Response({"sent_to": len(families), "invoice": nxt.title})


urlpatterns = [
    path("console/students", StudentDirectoryView.as_view()),
    path("console/students/export", StudentExportView.as_view()),
    path("console/students/message", StudentBulkMessageView.as_view()),
    path("console/students/<str:student_id>", StudentProfileView.as_view()),
    path("console/students/<str:student_id>/report-card.pdf", StudentReportCardView.as_view()),
    path("console/students/<str:student_id>/message", StudentMessageView.as_view()),
    path("console/students/<str:student_id>/remarks", StudentRemarkView.as_view()),
    path("console/students/<str:student_id>/fee-reminder", StudentFeeReminderView.as_view()),
]
