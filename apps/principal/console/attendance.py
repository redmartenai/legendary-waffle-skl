"""Console: attendance page (students and staff), absence alerts, calls and correction slips."""

from collections import Counter, defaultdict
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import AcademicYear, ClassGroup, Student, StudentGuardian
from apps.accounts.audit import audit
from apps.accounts.models import Department, Membership, Role
from apps.approvals.models import ApprovalRequest
from apps.approvals.services import decide, undo
from apps.attendance.models import AbsenceContact, AttendanceCorrection, AttendanceException, AttendanceSession, LeaveApplication
from apps.core.api import SchoolAPIView, parse_uuid
from apps.core.utils import school_now, school_today, school_tz
from apps.notifications.models import Category
from apps.notifications.services import notify
from apps.principal.views import PRE_PRIMARY, SUPPORT_ROLES, _grade_key, _pct, _school_days_back, chronic_absentees, register_today, staff_on_leave

from .common import CONSOLE_ROLES

ABSENT = ("absent", "excused")
LATE = ("late", "half_day")
HEAT_DAYS = 10


def _day_param(request) -> date:
    today = school_today(request.school)
    raw = request.query_params.get("date")
    try:
        day = date.fromisoformat(raw) if raw else today
    except (TypeError, ValueError) as exc:
        raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc
    if day > today:
        raise ValidationError({"date": "That day hasn't happened yet."})
    return day


def _short(label: str) -> str:
    return label.replace("Nursery", "Nur")


def _window(day: date, n: int = HEAT_DAYS) -> list[date]:
    """The last ``n`` school days ending with ``day``, oldest first."""
    return list(reversed([day] + _school_days_back(day, n - 1)))


def _explained(day: date) -> set:
    """Children whose absence on ``day`` has a reason: marked on leave, or a leave application covers the day."""
    excused = set(AttendanceException.objects.filter(session__date=day, status="excused").values_list("student_id", flat=True))
    leave = set(
        LeaveApplication.objects.filter(from_date__lte=day, to_date__gte=day)
        .exclude(status=LeaveApplication.Status.DECLINED)
        .values_list("student_id", flat=True)
    )
    return excused | leave


def _absentees(day: date) -> tuple[list, set]:
    absent = list(AttendanceException.objects.filter(session__date=day, status__in=ABSENT).values_list("student_id", flat=True))
    explained = _explained(day)
    return absent, {sid for sid in absent if sid not in explained}


def _alerted(day: date) -> set:
    return set(AbsenceContact.objects.filter(date=day, channel=AbsenceContact.Channel.ALERT).values_list("student_id", flat=True))


def _heat(day: date) -> dict:
    days = _window(day)
    groups = sorted(ClassGroup.objects.all(), key=lambda g: (_grade_key(g.grade), g.section))
    sizes = Counter(Student.objects.filter(is_active=True).values_list("class_group_id", flat=True))
    marked = set(AttendanceSession.objects.filter(date__in=days).values_list("class_group_id", "date"))
    absent = Counter(AttendanceException.objects.filter(session__date__in=days, status__in=ABSENT).values_list("session__class_group_id", "session__date"))
    rows = []
    for g in groups:
        size = sizes.get(g.id, 0)
        if not size:
            continue
        values = [round(_pct(size - absent[(g.id, d)], size)) if (g.id, d) in marked else None for d in days]
        rows.append({"id": str(g.id), "label": _short(g.short_label), "grade": g.grade, "values": values})
    split = [r for r in rows if r["grade"] in PRE_PRIMARY or (r["grade"].isdigit() and int(r["grade"]) <= 4)]
    rest = [r for r in rows if r not in split]
    return {
        "days": [d.isoformat() for d in days],
        "groups": [
            {"key": "junior", "from": split[0]["grade"] if split else None, "to": split[-1]["grade"] if split else None, "sections": split},
            {"key": "senior", "from": rest[0]["grade"] if rest else None, "to": rest[-1]["grade"] if rest else None, "sections": rest},
        ],
    }


def _insight(heat: dict) -> dict:
    """Sections under 90% on the last day, how long they've been under, and the floor for everyone else."""
    days = heat["days"]
    flagged, others = [], []
    for grp in heat["groups"]:
        for s in grp["sections"]:
            v = s["values"]
            if v[-1] is None:
                continue
            if v[-1] >= 90:
                others.append(v[-1])
                continue
            run = 0
            for x in reversed(v):
                if x is not None and x < 90:
                    run += 1
                else:
                    break
            before = [(x, days[i]) for i, x in enumerate(v[: len(v) - run]) if x is not None]
            peak = max(before, key=lambda t: t[0]) if before else (None, None)
            flagged.append({"label": s["label"], "today": v[-1], "run": run, "since": days[len(v) - run], "peak": peak[0], "peak_date": peak[1]})
    flagged.sort(key=lambda f: (-f["run"], f["today"]))
    return {"flagged": flagged, "floor": min(others) if others else None}


def _dots(student_ids, class_of: dict, days: list[date]) -> dict:
    """Register dots per student over ``days``: p present, a absent, l late, f not marked."""
    ex = {(sid, d): st for sid, d, st in AttendanceException.objects.filter(student_id__in=student_ids, session__date__in=days).values_list("student_id", "session__date", "status")}
    sessions = set(AttendanceSession.objects.filter(class_group_id__in=set(class_of.values()), date__in=days).values_list("class_group_id", "date"))
    out = {}
    for sid in student_ids:
        marks = []
        for d in days:
            st = ex.get((sid, d))
            if (class_of[sid], d) not in sessions:
                marks.append("f")
            elif st in ABSENT:
                marks.append("a")
            elif st in LATE:
                marks.append("l")
            else:
                marks.append("p")
        out[sid] = marks
    return out


def _ytd(student_ids, class_of: dict, day: date) -> dict:
    year = AcademicYear.objects.filter(is_current=True).first()
    start = year.starts_on if year else day - timedelta(days=365)
    held = Counter(AttendanceSession.objects.filter(class_group_id__in=set(class_of.values()), date__gte=start, date__lte=day).values_list("class_group_id", flat=True))
    missed = Counter(AttendanceException.objects.filter(student_id__in=student_ids, status__in=ABSENT, session__date__gte=start, session__date__lte=day).values_list("student_id", flat=True))
    return {sid: _pct(held[class_of[sid]] - missed[sid], held[class_of[sid]]) for sid in student_ids}


def _contact_status(student_ids, since: date) -> dict:
    """The latest contact about each child since ``since``."""
    out = {}
    for c in AbsenceContact.objects.filter(student_id__in=student_ids, date__gte=since).select_related("by").order_by("created_at"):
        out[c.student_id] = {"channel": c.channel, "outcome": c.outcome, "at": c.created_at.isoformat(), "by": c.by.full_name if c.by else None}
    return out


def _chronic(day: date) -> list[dict]:
    rows = chronic_absentees(day)
    if not rows:
        return []
    ids = [parse_uuid(r["id"]) for r in rows]
    students = {s.id: s for s in Student.objects.filter(id__in=ids)}
    class_of = {sid: students[sid].class_group_id for sid in ids}
    days = _window(day)
    dots = _dots(ids, class_of, days)
    ytd = _ytd(ids, class_of, day)
    contacts = _contact_status(ids, days[0])
    out = []
    for r, sid in zip(rows, ids):
        since = days[len(days) - r["days"]]
        out.append({**r, "roll_no": students[sid].roll_no, "since": since.isoformat(), "marks": dots[sid], "ytd": ytd[sid], "contact": contacts.get(sid)})
    return out


def _corrections() -> list[dict]:
    recent = timezone.now() - timedelta(minutes=10)
    reqs = (
        ApprovalRequest.objects.filter(kind=ApprovalRequest.Kind.ATTENDANCE)
        .filter(Q(status=ApprovalRequest.Status.PENDING) | Q(decided_at__gte=recent))
        .select_related("requested_by")
        .order_by("-created_at")
    )
    corr_ids = [r.target_id for r in reqs]
    corrs = {c.id: c for c in AttendanceCorrection.objects.filter(id__in=corr_ids).select_related("session__class_group")}
    names = {}
    for c in corrs.values():
        names.update({str(e["student_id"]): None for e in c.entries})
    for s in Student.objects.filter(id__in=[parse_uuid(k) for k in names]):
        names[str(s.id)] = s.full_name
    out = []
    for r in reqs:
        c = corrs.get(r.target_id)
        if c is None:
            continue
        first = c.entries[0] if c.entries else {}
        undo_until = r.decided_at + timedelta(minutes=10) if r.decided_at else None
        out.append(
            {
                "id": str(r.id),
                "code": c.code,
                "class": c.session.class_group.short_label,
                "date": c.session.date.isoformat(),
                "student": names.get(str(first.get("student_id"))),
                "more": max(0, len(c.entries) - 1),
                "from": first.get("from"),
                "to": first.get("to"),
                "reason": c.reason,
                "requested_by": r.requested_by.full_name if r.requested_by else None,
                "requested_at": r.created_at.isoformat(),
                "status": r.status,
                "undo_until": undo_until.isoformat() if undo_until and undo_until > timezone.now() else None,
            }
        )
    return out


def _late_grades(day: date) -> list[str]:
    """The one or two grades that account for most of the day's late arrivals, if any do."""
    late = Counter(AttendanceException.objects.filter(session__date=day, status__in=LATE).values_list("session__class_group__grade", flat=True))
    total = sum(late.values())
    if total < 4:
        return []
    top = late.most_common(2)
    if sum(n for _g, n in top) * 2 < total:
        return []
    return sorted((g for g, _n in top), key=_grade_key)


def students_payload(school, day: date) -> dict:
    today = school_today(school)
    reg = register_today(school, day)
    previous = _school_days_back(day, 1)[0]
    prev = register_today(school, previous)
    absent, unexplained = _absentees(day)
    alerted = _alerted(day)
    sessions = list(AttendanceSession.objects.filter(date=day).values_list("marked_at", flat=True))
    tz = school_tz(school)
    roll_at = None
    if sessions:
        mids = sorted(s.astimezone(tz) for s in sessions)
        roll_at = mids[len(mids) // 2].isoformat()
    in_school = reg["present"] + reg["late"]
    prev_in = prev["present"] + prev["late"]
    teachers = set(Membership.objects.filter(role=Role.TEACHER, is_active=True).values_list("user_id", flat=True))
    leave = [lv for lv in staff_on_leave(day) if lv.user_id in teachers]
    heat = _heat(day)
    return {
        "date": day.isoformat(),
        "today": today.isoformat(),
        "is_today": day == today,
        "registers": {"sections": reg["sections"], "marked": reg["sections_marked"], "last_marked_at": reg["last_marked_at"], "roll_at": roll_at},
        "roll": {
            "total": reg["total"],
            "marked_total": reg["marked_total"],
            "in_school": in_school,
            "percent": _pct(in_school, reg["marked_total"]),
            "previous_date": previous.isoformat(),
            "previous_percent": _pct(prev_in, prev["marked_total"]),
            "absent": len(absent),
            "absent_percent": _pct(len(absent), reg["marked_total"]),
            "explained": len(absent) - len(unexplained),
            "unexplained": len(unexplained),
            "late": reg["late"],
            "late_grades": _late_grades(day),
        },
        "alerts": {"pending": len(unexplained - alerted) if day == today else 0, "sent": len(unexplained & alerted), "enabled": bool(((school.settings or {}).get("notifications") or {}).get("absence_alerts", True))},
        "teachers": {"total": len(teachers), "in": len(teachers) - len({lv.user_id for lv in leave}), "on_leave": [lv.user.full_name for lv in leave]},
        "heat": heat,
        "insight": _insight(heat),
        "chronic": _chronic(day),
        "corrections": _corrections(),
    }


def staff_payload(school, day: date) -> dict:
    from apps.staff.models import StaffAttendance

    teacher_ids = set(Membership.objects.filter(role=Role.TEACHER, is_active=True).values_list("user_id", flat=True))
    support_ids = set(Membership.objects.filter(role__in=SUPPORT_ROLES, is_active=True).values_list("user_id", flat=True)) - teacher_ids
    everyone = teacher_ids | support_ids
    days = _window(day)
    rows = list(StaffAttendance.objects.filter(date__in=days, user_id__in=everyone).select_related("user"))
    today_rows = {r.user_id: r for r in rows if r.date == day}
    leave_ids = {lv.user_id for lv in staff_on_leave(day)}
    status = Counter()
    for uid in everyone:
        r = today_rows.get(uid)
        st = r.status if r else ("leave" if uid in leave_ids else "unmarked")
        status[st] += 1
    checkins = sorted(
        ({"id": str(r.user_id), "name": r.user.full_name, "initials": r.user.initials, "kind": "teacher" if r.user_id in teacher_ids else "support", "status": r.status, "check_in": r.check_in.strftime("%H:%M") if r.check_in else None, "check_out": r.check_out.strftime("%H:%M") if r.check_out else None, "source": r.source} for r in today_rows.values()),
        key=lambda x: (x["status"] == "present", x["check_in"] or "99", x["name"]),
    )
    by_day = defaultdict(Counter)
    for r in rows:
        by_day[(r.user_id in teacher_ids, r.date)][r.status] += 1
    heat = []
    for is_teacher, ids in ((True, teacher_ids), (False, support_ids)):
        values = []
        for d in days:
            c = by_day[(is_teacher, d)]
            marked = sum(c.values())
            values.append(round(_pct(c["present"] + c["late"], marked)) if marked else None)
        heat.append({"key": "teachers" if is_teacher else "support", "total": len(ids), "values": values})
    missing = [
        {"id": str(uid), "name": r.user.full_name, "status": r.status}
        for uid, r in today_rows.items()
        if r.status in ("absent", "leave")
    ]
    return {
        "date": day.isoformat(),
        "is_today": day == school_today(school),
        "marked": len(today_rows),
        "teachers": {"total": len(teacher_ids), "in": sum(1 for u in teacher_ids if today_rows.get(u) and today_rows[u].status in ("present", "late"))},
        "support": {"total": len(support_ids), "in": sum(1 for u in support_ids if today_rows.get(u) and today_rows[u].status in ("present", "late"))},
        "status": {k: status[k] for k in ("present", "late", "absent", "leave", "unmarked")},
        "days": [d.isoformat() for d in days],
        "heat": heat,
        "missing": sorted(missing, key=lambda m: m["name"]),
        "checkins": checkins,
    }


class AttendanceView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        day = _day_param(request)
        if request.query_params.get("view") == "staff":
            return Response(staff_payload(request.school, day))
        return Response(students_payload(request.school, day))


class AbsenteesView(SchoolAPIView):
    """Who is missing on a day, optionally in some sections (``sections=9-A,7-C``)."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        day = _day_param(request)
        labels = [s.strip() for s in (request.query_params.get("sections") or "").split(",") if s.strip()]
        qs = AttendanceException.objects.filter(session__date=day, status__in=ABSENT + LATE).select_related("student__class_group")
        if labels:
            ids = [g.id for g in ClassGroup.objects.all() if _short(g.short_label) in labels or g.short_label in labels]
            qs = qs.filter(session__class_group_id__in=ids)
        explained = _explained(day)
        alerted = _alerted(day)
        rows = sorted(qs, key=lambda e: (_grade_key(e.student.class_group.grade), e.student.class_group.section, e.student.roll_no))
        guardians = {}
        for link in StudentGuardian.objects.filter(student_id__in=[e.student_id for e in rows]).select_related("user").order_by("-is_primary"):
            guardians.setdefault(link.student_id, link.user)
        return Response(
            {
                "date": day.isoformat(),
                "items": [
                    {
                        "id": str(e.student_id),
                        "name": e.student.full_name,
                        "class": e.student.class_group.short_label,
                        "roll_no": e.student.roll_no,
                        "status": "late" if e.status in LATE else "absent",
                        "note": e.note,
                        "explained": e.student_id in explained,
                        "alerted": e.student_id in alerted,
                        "guardian": {"name": guardians[e.student_id].full_name, "phone": guardians[e.student_id].phone} if e.student_id in guardians else None,
                    }
                    for e in rows
                ],
            }
        )


def _template(school) -> str:
    from .settings import DEFAULT_ABSENCE_TEMPLATE

    return (((school.settings or {}).get("notifications") or {}).get("templates") or {}).get("absence") or DEFAULT_ABSENCE_TEMPLATE


class AbsenceAlertsView(SchoolAPIView):
    """Alert the guardians of today's unexplained absentees. Each child is alerted at most once a day."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        school = request.school
        day = school_today(school)
        if not (((school.settings or {}).get("notifications") or {}).get("absence_alerts", True)):
            raise ValidationError({"alerts": "Absence alerts are switched off in settings."})
        _absent, unexplained = _absentees(day)
        pending = unexplained - _alerted(day)
        if not pending:
            raise ValidationError({"alerts": "Every unexplained absence has already been alerted today."})
        students = {s.id: s for s in Student.objects.filter(id__in=pending).select_related("class_group")}
        links = defaultdict(list)
        for link in StudentGuardian.objects.filter(student_id__in=pending, receives_alerts=True).select_related("user"):
            links[link.student_id].append(link.user)
        template = _template(school)
        stamp = f"{day:%a} {day.day} {day:%b}"
        reached, sent = 0, 0
        with transaction.atomic():
            for sid, student in students.items():
                users = links.get(sid, [])
                created = notify(
                    users,
                    school=school,
                    category=Category.ATTENDANCE,
                    title=f"{student.full_name.split()[0]} is absent today",
                    body=template.replace("{child}", student.full_name).replace("{date}", stamp).replace("{class}", student.class_group.short_label),
                    data={"type": "absence_alert", "student_id": str(sid), "date": day.isoformat()},
                    dedupe_key=f"absence:{sid}:{day.isoformat()}",
                )
                reached += len(created)
                AbsenceContact.objects.create(student=student, date=day, channel=AbsenceContact.Channel.ALERT, outcome=AbsenceContact.Outcome.SENT, by=request.user, note=f"{len(users)} guardian(s)")
                sent += 1
            audit(request, "attendance.alert", target=("attendance", day.isoformat()), summary=f"Absence alerts sent for {sent} children ({reached} guardians)", detail={"students": sent, "guardians": reached})
        return Response({"students": sent, "guardians": reached, "payload": students_payload(school, day)})


class ContactView(SchoolAPIView):
    """Log a call to a family about an absence."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        student = Student.objects.filter(id=parse_uuid(request.data.get("student_id"), "student_id")).first()
        if student is None:
            raise Http404
        outcome = request.data.get("outcome")
        if outcome not in (AbsenceContact.Outcome.REACHED, AbsenceContact.Outcome.NO_ANSWER):
            raise ValidationError({"outcome": "Say whether you reached the family."})
        note = str(request.data.get("note", "")).strip()[:300]
        day = school_today(request.school)
        AbsenceContact.objects.create(student=student, date=day, channel=AbsenceContact.Channel.CALL, outcome=outcome, note=note, by=request.user)
        audit(request, "attendance.call", target=student, summary=f"Called {student.full_name}'s family: {'reached' if outcome == 'reached' else 'no answer'}")
        return Response(students_payload(request.school, day), status=201)


class HandoffView(SchoolAPIView):
    """Hand the chronic-absentee calls to the front office: they get a notification with the list."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        day = school_today(request.school)
        chronic = [c for c in chronic_absentees(day) if not c["reason_given"]]
        ids = [parse_uuid(c["id"]) for c in chronic]
        contacts = _contact_status(ids, day - timedelta(days=14))
        todo = [c for c, sid in zip(chronic, ids) if not (contacts.get(sid) and contacts[sid]["outcome"] == "reached") and not (contacts.get(sid) and contacts[sid]["channel"] == "handoff")]
        if not todo:
            raise ValidationError({"students": "There are no calls left to hand over."})
        office = [m.user for m in Membership.objects.filter(role=Role.ADMIN, department=Department.OFFICE, is_active=True).exclude(title="Support staff").select_related("user")]
        if not office:
            office = [m.user for m in Membership.objects.filter(role=Role.ADMIN, department=Department.OFFICE, is_active=True).select_related("user")[:1]]
        if not office:
            raise ValidationError({"students": "Nobody is set up in the front office."})
        names = ", ".join(f"{c['name']} ({c['class']})" for c in todo)
        with transaction.atomic():
            notify(
                office,
                school=request.school,
                category=Category.ATTENDANCE,
                title=f"{len(todo)} absence call{'s' if len(todo) != 1 else ''} from {request.user.full_name}",
                body=f"Please call the families of: {names}"[:500],
                data={"type": "absence_calls", "student_ids": [c["id"] for c in todo], "date": day.isoformat()},
                dedupe_key=f"absence-calls:{day.isoformat()}:{','.join(sorted(c['id'] for c in todo))}",
            )
            for c in todo:
                AbsenceContact.objects.create(student_id=parse_uuid(c["id"]), date=day, channel=AbsenceContact.Channel.HANDOFF, by=request.user, note=", ".join(u.full_name for u in office))
            audit(request, "attendance.handoff", target=("attendance", day.isoformat()), summary=f"{len(todo)} absence calls handed to the front office")
        return Response({"handed": len(todo), "to": [u.full_name for u in office], "payload": students_payload(request.school, day)})


class CorrectionDecisionView(SchoolAPIView):
    """Approve or reject a register correction slip (``decision``: approve / decline), or undo it."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, approval_id, verb):
        req = ApprovalRequest.objects.filter(id=approval_id, kind=ApprovalRequest.Kind.ATTENDANCE).first()
        if req is None:
            raise Http404
        device = request.META.get("HTTP_USER_AGENT", "")
        corr = req.target
        if verb == "undo":
            undo(req, request.user, device=device)
            audit(request, "approval.undo", target=req, module="attendance", summary=f"Undid decision on {corr.code}")
        else:
            decision = request.data.get("decision")
            if decision not in ("approve", "decline"):
                raise ValidationError({"decision": "Use approve or decline."})
            decide(req, decision, request.user, note=str(request.data.get("note", "")), device=device)
            audit(
                request,
                f"approval.{decision}",
                target=req,
                module="attendance",
                summary=f"{'Approved' if decision == 'approve' else 'Rejected'} {corr.code}: {corr.session.class_group.short_label} register, {corr.session.date:%d %b}",
            )
        day = school_today(request.school)
        return Response({"corrections": _corrections(), "now": school_now(request.school).isoformat(), "day": day.isoformat()})


urlpatterns = [
    path("console/attendance", AttendanceView.as_view()),
    path("console/attendance/absentees", AbsenteesView.as_view()),
    path("console/attendance/alerts", AbsenceAlertsView.as_view()),
    path("console/attendance/contacts", ContactView.as_view()),
    path("console/attendance/handoff", HandoffView.as_view()),
    path("console/attendance/corrections/<uuid:approval_id>/<str:verb>", CorrectionDecisionView.as_view()),
]
