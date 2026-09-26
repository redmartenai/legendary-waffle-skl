"""The principal's phone: the day's pulse, attendance for students and staff, and cover for absent teachers."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Avg, Sum
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import ClassGroup, Student, StudentGuardian, TeachingAssignment, TimetableSlot
from apps.accounts.models import MANAGEMENT_ROLES, Membership, Role
from apps.attendance.models import AttendanceException, AttendanceSession
from apps.core.api import SchoolAPIView
from apps.core.utils import mask_phone, school_now, school_today, school_tz
from apps.notifications.models import Category
from apps.notifications.services import notify
from apps.staff.models import StaffLeave, Substitution
from apps.staff.services import school_periods

PRE_PRIMARY = ("Nursery", "LKG", "UKG")
GRADE_ORDER = list(PRE_PRIMARY) + [str(g) for g in range(1, 13)]
SUPPORT_ROLES = (Role.ADMIN, Role.ACCOUNTANT, Role.TRANSPORT_MANAGER, Role.DRIVER, Role.ATTENDANT)


def _grade_key(grade: str) -> int:
    return GRADE_ORDER.index(grade) if grade in GRADE_ORDER else 99


def _wing(grade: str) -> str:
    if grade in PRE_PRIMARY:
        return "pre_primary"
    n = int(grade) if grade.isdigit() else 0
    return "primary" if n <= 5 else "middle" if n <= 8 else "secondary"


def _pct(part, whole) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


def _school_days_back(today: date, count: int) -> list[date]:
    days, d = [], today
    while len(days) < count:
        d -= timedelta(days=1)
        if d.weekday() != 6:
            days.append(d)
    return days


# ------------------------------------------------------------------ student register


def register_today(school, day: date) -> dict:
    """Present / late / absent for every section and grade, from the morning registers."""
    groups = {g.id: g for g in ClassGroup.objects.all()}
    sizes = Counter(Student.objects.filter(is_active=True).values_list("class_group_id", flat=True))
    sessions = {s.class_group_id: s for s in AttendanceSession.objects.filter(date=day)}
    exc = defaultdict(Counter)
    for gid, st in AttendanceException.objects.filter(session__date=day).values_list("session__class_group_id", "status"):
        exc[gid][st] += 1
    sections, grades = [], defaultdict(lambda: Counter())
    for gid, g in groups.items():
        total = sizes.get(gid, 0)
        if not total:
            continue
        absent = exc[gid]["absent"] + exc[gid]["excused"]
        late = exc[gid]["late"] + exc[gid]["half_day"]
        present = total - absent - late
        marked = gid in sessions
        sections.append({"id": str(gid), "label": g.short_label, "grade": g.grade, "total": total, "present": present, "late": late, "absent": absent, "marked": marked})
        if marked:
            bucket = grades[g.grade]
            bucket["total"] += total
            bucket["present"] += present
            bucket["late"] += late
            bucket["absent"] += absent
    by_grade = [
        {"grade": grade, "total": c["total"], "present": c["present"], "late": c["late"], "absent": c["absent"], "percent": _pct(c["present"], c["total"])}
        for grade, c in sorted(grades.items(), key=lambda kv: _grade_key(kv[0]))
    ]
    marked = [s for s in sections if s["marked"]]
    tot = Counter()
    for s in marked:
        tot.update({"total": s["total"], "present": s["present"], "late": s["late"], "absent": s["absent"]})
    last = max((s.marked_at for s in sessions.values()), default=None)
    return {
        "total": sum(s["total"] for s in sections),
        "marked_total": tot["total"],
        "present": tot["present"],
        "late": tot["late"],
        "absent": tot["absent"],
        "percent": _pct(tot["present"], tot["total"]),
        "sections": len(sections),
        "sections_marked": len(marked),
        "last_marked_at": last.isoformat() if last else None,
        "by_grade": by_grade,
        "by_section": sections,
    }


def average_present(day: date, days: int = 20) -> float | None:
    """Mean daily % present (on time) over the previous `days` school days."""
    back = _school_days_back(day, days)
    total = Student.objects.filter(is_active=True).count()
    if not total:
        return None
    rows = Counter(
        AttendanceException.objects.filter(session__date__in=back, status__in=["absent", "excused", "late", "half_day"]).values_list("session__date", flat=True)
    )
    marked_days = set(AttendanceSession.objects.filter(date__in=back).values_list("date", flat=True))
    values = [(total - rows.get(d, 0)) * 100 / total for d in marked_days]
    return round(sum(values) / len(values), 1) if values else None


def chronic_absentees(day: date, minimum: int = 3) -> list[dict]:
    """Children absent on each of the last N school days (N ≥ 3), longest first."""
    window = [day] + _school_days_back(day, 9)
    absent = defaultdict(set)
    for sid, d in AttendanceException.objects.filter(session__date__in=window, status__in=["absent", "excused"]).values_list("student_id", "session__date"):
        absent[sid].add(d)
    runs = {}
    for sid, dates in absent.items():
        n = 0
        for d in window:
            if d in dates:
                n += 1
            else:
                break
        if n >= minimum:
            runs[sid] = n
    if not runs:
        return []
    from apps.attendance.models import LeaveApplication

    students = {s.id: s for s in Student.objects.filter(id__in=runs).select_related("class_group")}
    on_leave = set(LeaveApplication.objects.filter(student_id__in=runs, from_date__lte=day, to_date__gte=day - timedelta(days=10)).values_list("student_id", flat=True))
    guardians = {}
    for link in StudentGuardian.objects.filter(student_id__in=runs).select_related("user").order_by("-is_primary"):
        guardians.setdefault(link.student_id, link.user)
    out = []
    for sid, n in sorted(runs.items(), key=lambda kv: -kv[1]):
        s = students[sid]
        g = guardians.get(sid)
        out.append(
            {
                "id": str(sid),
                "name": s.full_name,
                "initials": s.initials,
                "class": s.class_group.short_label,
                "days": n,
                "reason_given": sid in on_leave,
                "guardian": {"user_id": str(g.id), "name": g.full_name, "phone": g.phone, "phone_masked": mask_phone(g.phone)} if g else None,
            }
        )
    return out


# ------------------------------------------------------------------ staff and cover


def staff_on_leave(day: date) -> list:
    return list(StaffLeave.objects.filter(status=StaffLeave.Status.APPROVED, from_date__lte=day, to_date__gte=day).select_related("user"))


def cover_board(school, day: date) -> dict:
    """Every period today taught by someone on leave: who covers it, or that nobody does yet."""
    now = school_now(school)
    tz = school_tz(school)
    weekday = day.weekday()
    leaves = staff_on_leave(day)
    absent_ids = {lv.user_id for lv in leaves}
    slots = list(
        TimetableSlot.objects.filter(teacher_id__in=absent_ids, weekday=weekday).select_related("class_group", "subject", "teacher").order_by("period")
    )
    covered = {s.slot_id: s for s in Substitution.objects.filter(date=day, slot__in=slots).select_related("teacher")}
    teachers = {m.user_id: m.user for m in Membership.objects.filter(role=Role.TEACHER, is_active=True).select_related("user")}
    busy = defaultdict(set)
    for tid, period in TimetableSlot.objects.filter(weekday=weekday).values_list("teacher_id", "period"):
        busy[period].add(tid)
    for tid, period in Substitution.objects.filter(date=day).values_list("teacher_id", "slot__period"):
        busy[period].add(tid)
    subjects = {}
    for a in TeachingAssignment.objects.select_related("subject"):
        subjects.setdefault(a.teacher_id, a.subject.name)

    periods = []
    for period, starts, ends in school_periods(weekday):
        start = datetime.combine(day, datetime.strptime(starts, "%H:%M").time(), tzinfo=tz)
        end = datetime.combine(day, datetime.strptime(ends, "%H:%M").time(), tzinfo=tz)
        state = "done" if now >= end else "now" if now >= start else "todo"
        if now.date() != day:
            state = "done" if day < now.date() else "todo"
        mine = [s for s in slots if s.period == period]
        open_slots = [s for s in mine if s.id not in covered]
        free = [
            {"id": str(uid), "name": u.full_name, "subject": subjects.get(uid)}
            for uid, u in sorted(teachers.items(), key=lambda kv: kv[1].full_name)
            if uid not in busy[period] and uid not in absent_ids
        ]
        periods.append(
            {
                "period": period,
                "starts_at": starts,
                "ends_at": ends,
                "state": state,
                "slots": [
                    {
                        "id": str(s.id),
                        "class": s.class_group.short_label,
                        "subject": s.subject.name,
                        "room": s.room,
                        "teacher": s.teacher.full_name,
                        "covered_by": covered[s.id].teacher.full_name if s.id in covered else None,
                    }
                    for s in mine
                ],
                "open": len(open_slots),
                "free": free if open_slots else [],
            }
        )
    # Periods already over can't be covered any more; only count what's still ahead (or on now).
    open_now = sum(p["open"] for p in periods if p["state"] != "done")
    return {
        "date": day.isoformat(),
        "on_leave": [
            {
                "id": str(lv.user_id),
                "name": lv.user.full_name,
                "initials": lv.user.initials,
                "subject": subjects.get(lv.user_id),
                "leave_kind": lv.kind,
                "uncovered": sum(1 for s in slots if s.teacher_id == lv.user_id and s.id not in covered),
            }
            for lv in leaves
        ],
        "periods": periods,
        "open": open_now,
        "free_teachers": len({f["id"] for p in periods for f in p["free"]}),
    }


class CoverView(SchoolAPIView):
    """GET today's cover board; POST assigns a free teacher to an uncovered period."""

    allowed_roles = MANAGEMENT_ROLES

    def get(self, request):
        return Response(cover_board(request.school, school_today(request.school)))

    def post(self, request):
        today = school_today(request.school)
        slot = TimetableSlot.objects.filter(id=request.data.get("slot_id")).select_related("class_group", "subject", "teacher").first()
        if slot is None or slot.weekday != today.weekday():
            raise Http404
        if slot.teacher_id not in {lv.user_id for lv in staff_on_leave(today)}:
            raise ValidationError({"slot_id": "This period's teacher isn't on leave today."})
        if Substitution.objects.filter(date=today, slot=slot).exists():
            raise ValidationError({"slot_id": "This period already has cover."})
        teacher = next((m.user for m in Membership.objects.filter(user_id=request.data.get("teacher_id"), role=Role.TEACHER, is_active=True).select_related("user")), None)
        if teacher is None:
            raise ValidationError({"teacher_id": "Choose a teacher."})
        clash = TimetableSlot.objects.filter(teacher=teacher, weekday=today.weekday(), period=slot.period).exists() or Substitution.objects.filter(
            date=today, teacher=teacher, slot__period=slot.period
        ).exists()
        if clash:
            raise ValidationError({"teacher_id": f"{teacher.full_name} is teaching then."})
        sub = Substitution.objects.create(
            date=today, slot=slot, teacher=teacher, absent_teacher=slot.teacher, reason="on leave", assigned_by=request.user, assigned_at=timezone.now()
        )
        notify(
            [teacher],
            school=request.school,
            category=Category.GENERAL,
            title=f"Cover: {slot.class_group.short_label} {slot.subject.name}, P{slot.period}",
            body=f"For {slot.teacher.full_name} · {slot.starts_at:%I:%M %p} · {slot.room}".replace(" 0", " "),
            data={"type": "cover", "cover_id": str(sub.id)},
        )
        return Response(cover_board(request.school, today), status=201)


# ------------------------------------------------------------------ attendance screen


class PrincipalAttendanceView(SchoolAPIView):
    allowed_roles = MANAGEMENT_ROLES

    def get(self, request):
        today = school_today(request.school)
        raw = request.query_params.get("date")
        try:
            day = date.fromisoformat(raw) if raw else today
        except ValueError as exc:
            raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc
        reg = register_today(request.school, day)
        avg = average_present(day)
        # Pre-primary is shown as one row.
        rows, pre = [], Counter()
        for g in reg["by_grade"]:
            if g["grade"] in PRE_PRIMARY:
                pre.update({"total": g["total"], "present": g["present"], "absent": g["absent"], "late": g["late"]})
            else:
                rows.append({**g, "label": g["grade"]})
        if pre["total"]:
            rows.insert(0, {"grade": "pre_primary", "label": "pre_primary", "total": pre["total"], "present": pre["present"], "late": pre["late"], "absent": pre["absent"], "percent": _pct(pre["present"], pre["total"])})

        # Staff: teachers and support staff, by wing (a teacher's wing is the grade they teach most).
        teacher_ids = set(Membership.objects.filter(role=Role.TEACHER, is_active=True).values_list("user_id", flat=True))
        support_ids = set(Membership.objects.filter(role__in=SUPPORT_ROLES, is_active=True).values_list("user_id", flat=True)) - teacher_ids
        leave_ids = {lv.user_id for lv in staff_on_leave(day)}
        wing_of = {}
        for tid, grade in TimetableSlot.objects.filter(teacher_id__in=teacher_ids).values_list("teacher_id", "class_group__grade"):
            wing_of.setdefault(tid, Counter())[_wing(grade)] += 1
        wings = Counter()
        wing_leave = Counter()
        for tid in teacher_ids:
            w = wing_of[tid].most_common(1)[0][0] if tid in wing_of else "secondary"
            wings[w] += 1
            if tid in leave_ids:
                wing_leave[w] += 1
        teachers_present = len(teacher_ids - leave_ids)
        return Response(
            {
                "date": day.isoformat(),
                "updated_at": timezone.now().isoformat(),
                "students": {**{k: v for k, v in reg.items() if k != "by_section"}, "average_20": avg, "by_grade": rows},
                "chronic": chronic_absentees(day),
                "staff": {
                    "teachers": len(teacher_ids),
                    "present": teachers_present,
                    "on_leave": len(teacher_ids & leave_ids),
                    "percent": _pct(teachers_present, len(teacher_ids)),
                    "support": len(support_ids),
                    "support_present": len(support_ids - leave_ids),
                    "by_wing": [
                        {"wing": w, "total": wings[w], "on_leave": wing_leave[w], "percent": _pct(wings[w] - wing_leave[w], wings[w])}
                        for w in ("pre_primary", "primary", "middle", "secondary")
                        if wings[w]
                    ],
                },
                "cover": cover_board(request.school, day) if day == today else None,
            }
        )


class AbsenteeMessageView(SchoolAPIView):
    """Send one message to the parents of the chosen absent children."""

    allowed_roles = MANAGEMENT_ROLES

    def post(self, request):
        body = str(request.data.get("body", "")).strip()
        ids = request.data.get("student_ids") or []
        if not body:
            raise ValidationError({"body": "Write the message."})
        students = list(Student.objects.filter(id__in=ids, is_active=True))
        if not students:
            raise ValidationError({"student_ids": "Choose at least one child."})
        users = {link.user for link in StudentGuardian.objects.filter(student__in=students).select_related("user")}
        notify(users, school=request.school, category=Category.ATTENDANCE, title=f"A note from {request.user.full_name}", body=body[:500], data={"type": "principal_note"})
        return Response({"sent_to": len(users)})


# ------------------------------------------------------------------ pulse


def _fees(school, today: date) -> dict:
    from apps.fees.models import FeeInvoice

    terms = (school.settings or {}).get("terms") or []
    term = next((t for t in terms if t.get("starts_on", "") <= today.isoformat() <= t.get("ends_on", "")), None)
    invoices = FeeInvoice.objects.all()
    if term:
        invoices = invoices.filter(due_date__gte=term["starts_on"], due_date__lte=term["ends_on"])
    billed = invoices.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    paid = invoices.aggregate(s=Sum("paid_amount"))["s"] or Decimal("0")
    overdue = sum((i.amount - i.paid_amount for i in invoices.filter(due_date__lt=today) if i.amount > i.paid_amount), Decimal("0"))
    target = ((school.settings or {}).get("fees") or {}).get("target") or {"percent": 90, "by": term["ends_on"] if term else None}
    return {"term": term["name"] if term else None, "billed": str(billed), "collected": str(paid), "overdue": str(overdue), "percent": _pct(float(paid), float(billed)), "target": target}


def _exams(school) -> dict | None:
    from apps.results.models import Exam, ExamMark

    names = list(Exam.objects.filter(is_published=True).values_list("name", flat=True).distinct())
    order = sorted(set(names), key=lambda n: Exam.objects.filter(name=n).order_by("held_on").values_list("held_on", flat=True).first())
    if not order:
        return None

    def pct(name, **filt):
        row = ExamMark.objects.filter(exam__name=name, exam__is_published=True, is_absent=False, **filt).aggregate(m=Avg("marks"), o=Avg("max_marks"))
        return round(float(row["m"]) * 100 / float(row["o"]), 1) if row["m"] is not None else None

    last = order[-1]
    prev = order[-2] if len(order) > 1 else None
    grades = sorted({g for g in ClassGroup.objects.values_list("grade", flat=True) if g.isdigit()}, key=int)
    worst = min(((g, pct(last, exam__class_group__grade=g)) for g in grades), key=lambda kv: (kv[1] is None, kv[1] or 0), default=(None, None))
    return {"exam": last, "average": pct(last), "previous_exam": prev, "previous": pct(prev) if prev else None, "lowest_grade": worst[0], "lowest": worst[1]}


def _transport(school) -> dict | None:
    from apps.transport import services as transport
    from apps.transport.models import Direction, Trip, Vehicle

    transport.ensure_today_trips(school)
    today = school_today(school)
    now = school_now(school)
    trips = list(Trip.objects.filter(service_date=today, direction=Direction.DROP).select_related("route", "vehicle", "school", "driver", "attendant").prefetch_related("route__stops"))
    if not trips:
        return None
    rows = []
    for trip in trips:
        live = transport.live_state(trip, staff=True)
        delay = live.get("delay_minutes") or 0
        start = datetime.combine(today, trip.scheduled_start, tzinfo=school_tz(school))
        # A bus still at school after its start time is late — but only while that's news (the first hour).
        if trip.status == Trip.Status.SCHEDULED and start < now <= start + timedelta(hours=1):
            delay = max(delay, int((now - start).total_seconds() // 60))
        rows.append((delay, trip, live))
    delay, trip, live = max(rows, key=lambda r: r[0])
    stops = live["stops"]
    out = sum(1 for _d, t, _l in rows if t.status == Trip.Status.ACTIVE)
    idle = Vehicle.objects.filter(is_active=True).exclude(id__in=[t.vehicle_id for t in trips]).first()
    return {
        "route_id": str(trip.route_id),
        "route": trip.route.name,
        "direction": trip.direction,
        "status": trip.status,
        "delay_minutes": delay,
        "leaves_at": (datetime.combine(today, trip.scheduled_start, tzinfo=school_tz(school)) + timedelta(minutes=delay)).strftime("%H:%M"),
        "stops": [{"name": s["name"], "status": s["status"]} for s in stops],
        "buses_out": out,
        "buses_total": Vehicle.objects.filter(is_active=True).count(),
        "idle_vehicle": idle.label if idle else None,
    }


class PulseView(SchoolAPIView):
    """Everything the principal's home screen shows, in one request."""

    allowed_roles = MANAGEMENT_ROLES

    def get(self, request):
        from apps.approvals.models import ApprovalRequest
        from apps.approvals.services import payload
        from apps.results.models import ExamPaper

        today = school_today(request.school)
        reg = register_today(request.school, today)
        # Lowest grade, and the section that pulls it down, compared with its best day in the last fortnight.
        lowest = None
        graded = [g for g in reg["by_grade"] if g["percent"] is not None]
        if graded:
            g = min(graded, key=lambda x: x["percent"])
            sections = [s for s in reg["by_section"] if s["grade"] == g["grade"] and s["marked"]]
            worst = min(sections, key=lambda s: s["present"] / s["total"]) if sections else None
            best = None
            if worst:
                size = worst["total"]
                history = Counter(
                    AttendanceException.objects.filter(
                        session__class_group_id=worst["id"], session__date__in=_school_days_back(today, 10), status__in=["absent", "excused", "late", "half_day"]
                    ).values_list("session__date", flat=True)
                )
                days = AttendanceSession.objects.filter(class_group_id=worst["id"], date__in=_school_days_back(today, 10)).values_list("date", flat=True)
                scored = [(d, _pct(size - history.get(d, 0), size)) for d in days]
                best = max(scored, key=lambda x: x[1]) if scored else None
            lowest = {
                "grade": g["grade"],
                "percent": g["percent"],
                "section": worst["label"] if worst else None,
                "section_percent": _pct(worst["present"], worst["total"]) if worst else None,
                "best_percent": best[1] if best else None,
                "best_date": best[0].isoformat() if best else None,
            }
        pending = list(ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING).select_related("requested_by"))
        from apps.approvals.views import urgency

        pending.sort(key=urgency(today))
        exam_day = ExamPaper.objects.filter(date__gte=today, exam__is_published=False).order_by("date").values_list("date", "exam__name").first()
        school_days = sum(1 for i in range(1, (exam_day[0] - today).days + 1) if (today + timedelta(days=i)).weekday() != 6) if exam_day else None
        return Response(
            {
                "now": school_now(request.school).isoformat(),
                "register": {k: v for k, v in reg.items() if k != "by_section"},
                "lowest": lowest,
                "chronic": len(chronic_absentees(today)),
                "cover": cover_board(request.school, today),
                "intray": {
                    "total": len(pending),
                    "top": payload(pending[0]) if pending else None,
                    "next": [
                        {"id": str(r.id), "kind": r.kind, "name": r.requested_by.full_name if r.requested_by else "", "summary": r.summary} for r in pending[1:3]
                    ],
                },
                "transport": _transport(request.school),
                "fees": _fees(request.school, today),
                "exams": _exams(request.school),
                "next_exam": {"name": exam_day[1], "date": exam_day[0].isoformat(), "school_days": school_days} if exam_day else None,
            }
        )


class SectionsView(SchoolAPIView):
    """Every section, for picking a broadcast audience."""

    allowed_roles = MANAGEMENT_ROLES

    def get(self, request):
        groups = sorted(ClassGroup.objects.all(), key=lambda g: (_grade_key(g.grade), g.section))
        sizes = Counter(Student.objects.filter(is_active=True).values_list("class_group_id", flat=True))
        return Response(
            {
                "grades": sorted({g.grade for g in groups}, key=_grade_key),
                "sections": [{"id": str(g.id), "short_label": g.short_label, "grade": g.grade, "students": sizes.get(g.id, 0)} for g in groups],
            }
        )
