"""Console: staff — today's check-ins, leave, workload, open positions and the staff directory."""

import csv
import io
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.http import Http404, HttpResponse
from django.urls import path
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import ClassGroup, Subject, TeachingAssignment, TimetableSlot
from apps.accounts.audit import audit
from apps.accounts.models import Department, Membership, Role, User
from apps.approvals.models import ApprovalRequest
from apps.core.api import SchoolAPIView
from apps.core.utils import normalize_phone, school_now, school_today, school_tz
from apps.principal.views import _grade_key, _pct, cover_board
from apps.staff.models import StaffAttendance, StaffLeave, StaffProfile, Substitution, Vacancy

from .academics import policy as academic_policy
from .common import CONSOLE_ROLES, current_term

PAGE_SIZE = 10
TEACHING_ROLES = (Role.TEACHER,)
SUPPORT_ROLES = (Role.ADMIN, Role.ACCOUNTANT, Role.TRANSPORT_MANAGER, Role.DRIVER, Role.ATTENDANT)
LEAVE_WORD = {"casual": "personal", "sick": "medical", "earned": "earned"}


def staff_members(tab: str) -> dict:
    """user id -> Membership for the tab (teaching or support); a person counts once."""
    roles = TEACHING_ROLES if tab == "teaching" else SUPPORT_ROLES
    teaching_ids = set(Membership.objects.filter(role=Role.TEACHER, is_active=True).values_list("user_id", flat=True))
    out = {}
    for m in Membership.objects.filter(role__in=roles, is_active=True).select_related("user").order_by("user__full_name"):
        if tab == "support" and m.user_id in teaching_ids:
            continue
        out.setdefault(m.user_id, m)
    return out


def _term_start(school, today: date) -> date:
    term = current_term(school, today)
    return date.fromisoformat(term["starts_on"]) if term else today - timedelta(days=180)


def attendance_rates(user_ids, since: date, today: date) -> dict:
    """user id -> % of working days present (on time or late) since ``since``; approved leave isn't counted."""
    tally = defaultdict(Counter)
    for uid, status in StaffAttendance.objects.filter(user_id__in=user_ids, date__gte=since, date__lte=today).values_list("user_id", "status"):
        tally[uid][status] += 1
    out = {}
    for uid in user_ids:
        c = tally.get(uid, Counter())
        worked = c["present"] + c["late"]
        out[uid] = _pct(worked, worked + c["absent"])
    return out


def _slot_now(school, day: date):
    now = school_now(school)
    if now.date() != day:
        return None
    t = now.time()
    return {s.teacher_id: s for s in TimetableSlot.objects.filter(weekday=day.weekday(), starts_at__lte=t, ends_at__gt=t).select_related("class_group")}


def today_states(school, members: dict, today: date) -> dict:
    tz = school_tz(school)
    ids = list(members)
    marks = {a.user_id: a for a in StaffAttendance.objects.filter(user_id__in=ids, date=today)}
    leaves = {lv.user_id: lv for lv in StaffLeave.objects.filter(user_id__in=ids, status="approved", from_date__lte=today, to_date__gte=today)}
    now = school_now(school)
    subs = defaultdict(list)
    for s in Substitution.objects.filter(date=today, teacher_id__in=ids).select_related("slot", "slot__class_group", "absent_teacher").order_by("slot__period"):
        subs[s.teacher_id].append(s)
    teaching_now = _slot_now(school, today) or {}
    out = {}
    for uid in ids:
        lv = leaves.get(uid)
        mark = marks.get(uid)
        if lv:
            out[uid] = {
                "state": "leave",
                "leave_kind": LEAVE_WORD.get(lv.kind, lv.kind),
                "from_date": lv.from_date.isoformat(),
                "to_date": lv.to_date.isoformat(),
            }
            continue
        if mark is None:
            out[uid] = {"state": "not_in"}
            continue
        if mark.status == "absent":
            out[uid] = {"state": "absent"}
            continue
        base = {"at": mark.check_in.strftime("%H:%M") if mark.check_in else None, "late": mark.status == "late"}
        cover = next((s for s in subs.get(uid, []) if datetime.combine(today, s.slot.ends_at, tzinfo=tz) > now), None)
        if cover is not None:
            out[uid] = {
                **base,
                "state": "substituting",
                "class": cover.slot.class_group.short_label,
                "period": cover.slot.period,
                "for": cover.absent_teacher.full_name.split()[0] if cover.absent_teacher else None,
            }
        elif uid in teaching_now:
            out[uid] = {**base, "state": "teaching", "class": teaching_now[uid].class_group.short_label}
        else:
            out[uid] = {**base, "state": "late" if mark.status == "late" else "present"}
    return out


def _classes(uid, taught: dict, led: dict) -> dict:
    groups = sorted(taught.get(uid, set()), key=lambda g: (_grade_key(g[0]), g[1]))
    labels = [f"{g}-{s}" for g, s in groups]
    grades = sorted({g for g, _s in groups}, key=_grade_key)
    return {
        "labels": labels,
        "grades": [grades[0], grades[-1]] if grades else None,
        "sections": len(labels),
        "class_teacher_of": led.get(uid),
    }


def _row(m: Membership, profile, subject, classes, rate, load, cap, state) -> dict:
    title = m.title or ""
    role_note = ""
    if " · " in title:
        role_note = title.split(" · ", 1)[1]
    elif classes and classes.get("class_teacher_of"):
        role_note = f"Class teacher · {classes['class_teacher_of']}"
    if classes and classes.get("class_teacher_of") and "Class teacher" in role_note:
        role_note = f"Class teacher · {classes['class_teacher_of']}"
    teaching = m.role == Role.TEACHER
    return {
        "id": str(m.user_id),
        "name": m.user.full_name,
        "initials": m.user.initials,
        "employee_id": profile.employee_id if profile else None,
        "role": m.role,
        "subject": (subject or (profile.designation if profile else None) or title.split(" · ")[0] or m.get_role_display()) if teaching else (title or m.get_role_display()),
        "role_note": role_note if teaching else (m.get_department_display() if m.department else m.get_role_display()),
        "classes": classes if teaching else None,
        "attendance": rate,
        "periods": load if teaching else None,
        "over_cap": bool(teaching and load > cap),
        "today": state,
        "phone": m.user.phone,
    }


def directory(request, members: dict, today: date) -> list[dict]:
    school = request.school
    ids = list(members)
    profiles = {p.user_id: p for p in StaffProfile.objects.filter(user_id__in=ids)}
    subjects = {}
    taught = defaultdict(set)
    for a in TeachingAssignment.objects.filter(teacher_id__in=ids).select_related("subject", "class_group").order_by("created_at"):
        subjects.setdefault(a.teacher_id, a.subject.name)
        taught[a.teacher_id].add((a.class_group.grade, a.class_group.section))
    led = {g.class_teacher_id: g.short_label for g in ClassGroup.objects.filter(class_teacher_id__in=ids, academic_year__is_current=True)}
    loads = Counter(TimetableSlot.objects.filter(teacher_id__in=ids).values_list("teacher_id", flat=True))
    cap = academic_policy(school)["max_periods"]
    rates = attendance_rates(ids, _term_start(school, today), today)
    states = today_states(school, members, today)
    return [
        _row(m, profiles.get(uid), subjects.get(uid), _classes(uid, taught, led), rates.get(uid), loads.get(uid, 0), cap, states[uid])
        for uid, m in members.items()
    ]


def _filter(rows, params) -> list:
    q = (params.get("q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in r["name"].lower() or q in (r["subject"] or "").lower() or q in (r["employee_id"] or "").lower()]
    if params.get("subject"):
        rows = [r for r in rows if r["subject"] == params["subject"]]
    state = params.get("today")
    if state:
        groups = {"in": {"present", "late", "teaching", "substituting"}, "leave": {"leave"}, "late": {"late"}, "substituting": {"substituting"}, "absent": {"absent", "not_in"}}
        wanted = groups.get(state, {state})
        rows = [r for r in rows if r["today"]["state"] in wanted or (state == "late" and r["today"].get("late"))]
    return rows


def _leave_requests(today: date) -> list[dict]:
    # Leave that starts on the next school day (Sunday is off) has to be decided today.
    next_day = today + timedelta(days=2 if today.weekday() == 5 else 1)
    ct = ContentType.objects.get_for_model(StaffLeave)
    reqs = list(ApprovalRequest.objects.filter(kind="leave", status=ApprovalRequest.Status.PENDING, target_type=ct).select_related("requested_by").order_by("due_on", "created_at"))
    leaves = {lv.id: lv for lv in StaffLeave.objects.filter(id__in=[r.target_id for r in reqs]).select_related("user")}
    support = set(Membership.objects.filter(role__in=SUPPORT_ROLES, is_active=True).exclude(user__memberships__role=Role.TEACHER).values_list("user_id", flat=True))
    out = []
    for r in reqs:
        lv = leaves.get(r.target_id)
        if lv is None:
            continue
        out.append(
            {
                "id": str(r.id),
                "user_id": str(lv.user_id),
                "name": lv.user.full_name,
                "initials": lv.user.initials,
                "leave_kind": LEAVE_WORD.get(lv.kind, lv.kind),
                "from_date": lv.from_date.isoformat(),
                "to_date": lv.to_date.isoformat(),
                "days": float(lv.days),
                "half_day": lv.half_day,
                "reason": lv.reason,
                "created_at": r.created_at.isoformat(),
                "decide_today": bool(r.due_on and r.due_on <= next_day),
                "support": lv.user_id in support,
            }
        )
    return out


def _leave_week(today: date, members: dict) -> dict:
    monday = today - timedelta(days=today.weekday())
    days = [monday + timedelta(days=i) for i in range(6)]
    ids = list(members)
    approved = list(StaffLeave.objects.filter(user_id__in=ids, status="approved", from_date__lte=days[-1], to_date__gte=monday))
    pending = list(StaffLeave.objects.filter(user_id__in=ids, status="pending", from_date__lte=days[-1], to_date__gte=monday))

    def on(leaves, d):
        return len({lv.user_id for lv in leaves if lv.from_date <= d <= lv.to_date})

    today_off = [lv for lv in approved if lv.from_date <= today <= lv.to_date]
    return {
        "from": days[0].isoformat(),
        "to": days[-1].isoformat(),
        "days": [{"date": d.isoformat(), "approved": on(approved, d), "pending": on(pending, d)} for d in days],
        "today": [{"id": str(lv.user_id), "name": members[lv.user_id].user.full_name, "initials": members[lv.user_id].user.initials} for lv in today_off],
    }


def _trend(members: dict, today: date, since: date) -> dict:
    ids = list(members)
    per_day = defaultdict(Counter)
    for d, status in StaffAttendance.objects.filter(user_id__in=ids, date__gte=since, date__lte=today).values_list("date", "status"):
        per_day[d][status] += 1
    total = len(ids) or 1
    series = [(d, (c["present"] + c["late"]) * 100 / total) for d, c in sorted(per_day.items())]
    last = series[-20:]
    return {
        "days": [{"date": d.isoformat(), "percent": round(p, 1)} for d, p in last],
        "term_average": round(sum(p for _d, p in series) / len(series), 1) if series else None,
        "today": round(last[-1][1], 1) if last and last[-1][0] == today else None,
    }


class StaffView(SchoolAPIView):
    """GET the page for a tab (``?tab=teaching|support``); ``?q=&subject=&today=&page=`` filter the table and
    ``?person=<user id>`` jumps to that person's page. POST adds a member of staff."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        today = school_today(school)
        params = request.query_params
        tab = params.get("tab") or "teaching"
        if tab not in ("teaching", "support"):
            raise ValidationError({"tab": "Use teaching or support."})
        person = params.get("person")
        if person:
            try:
                uuid.UUID(person)
            except ValueError as exc:
                raise ValidationError({"person": "Unknown person."}) from exc
            if person and not Membership.objects.filter(user_id=person, role__in=TEACHING_ROLES if tab == "teaching" else SUPPORT_ROLES).exists():
                other = "support" if tab == "teaching" else "teaching"
                if Membership.objects.filter(user_id=person, role__in=TEACHING_ROLES if other == "teaching" else SUPPORT_ROLES).exists():
                    tab = other
        members = staff_members(tab)
        everyone = directory(request, members, today)
        facets = sorted({r["subject"] for r in everyone if r["subject"]})
        rows = _filter(everyone, params)
        if params.get("sort") == "name":
            rows.sort(key=lambda r: r["name"])
        elif params.get("sort") == "-name":
            rows.sort(key=lambda r: r["name"], reverse=True)
        else:
            # Roster order: by employee ID, so the longest-serving come first.
            rows.sort(key=lambda r: (r["employee_id"] or "~", r["name"]))
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        try:
            page = max(1, int(params.get("page") or 1))
        except ValueError as exc:
            raise ValidationError({"page": "Use a whole number."}) from exc
        if person:
            index = next((i for i, r in enumerate(rows) if r["id"] == person), None)
            if index is not None:
                page = index // PAGE_SIZE + 1
        page = min(page, pages)

        in_today = [r for r in everyone if r["today"]["state"] in ("present", "late", "teaching", "substituting")]
        late = [r for r in everyone if r["today"].get("late")]
        times = [r["today"]["at"] for r in in_today if r["today"].get("at")]
        loads = [r["periods"] for r in everyone if r["periods"] is not None]
        cap = academic_policy(school)["max_periods"]
        board = cover_board(school, today) if tab == "teaching" and today.weekday() < 6 else {"open": 0}
        vacancies = list(Vacancy.objects.filter(status=Vacancy.Status.OPEN, kind=tab))
        term = current_term(school, today)
        since = _term_start(school, today)
        counts = {"teaching": len(members) if tab == "teaching" else len(staff_members("teaching")), "support": len(members) if tab == "support" else len(staff_members("support"))}
        requests = _leave_requests(today)
        return Response(
            {
                "tab": tab,
                "counts": counts,
                "summary": {
                    "total": counts["teaching"] + counts["support"],
                    "closes_at": (school.settings or {}).get("staff_attendance", {}).get("closes_at", "08:15"),
                    "term": {"name": term["name"], "week": term["week"]} if term else None,
                },
                "kpis": {
                    "present": {"count": len(in_today), "of": len(everyone), "percent": _pct(len(in_today), len(everyone)), "late": len(late), "last_in": max(times) if times else None},
                    "leave": {
                        "count": sum(1 for r in everyone if r["today"]["state"] == "leave"),
                        "people": [{"id": r["id"], "initials": r["initials"], "name": r["name"]} for r in everyone if r["today"]["state"] == "leave"],
                        "uncovered": board.get("open", 0),
                    },
                    "workload": {"average": round(sum(loads) / len(loads)) if loads else None, "cap": cap, "over_cap": sum(1 for x in loads if x > cap)},
                    "vacancies": {
                        "positions": sum(v.positions for v in vacancies),
                        "applicants": sum(v.applicants for v in vacancies),
                        "items": [{"id": str(v.id), "title": v.title, "note": v.note, "applicants": v.applicants, "positions": v.positions, "opened_on": v.opened_on.isoformat(), "closes_on": v.closes_on.isoformat() if v.closes_on else None} for v in vacancies],
                    },
                },
                "facets": {"subjects": facets},
                "page": page,
                "pages": pages,
                "page_size": PAGE_SIZE,
                "total": len(rows),
                "highlight": person if person and any(r["id"] == person for r in rows) else None,
                "items": rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
                "leave_week": _leave_week(today, members),
                "leave_requests": requests,
                "trend": _trend(members, today, since),
            }
        )

    def post(self, request):
        data = request.data
        errors = {}
        name = " ".join(str(data.get("full_name") or "").split())
        if len(name) < 2:
            errors["full_name"] = "Enter their full name."
        try:
            phone = normalize_phone(data.get("phone"))
        except ValueError as exc:
            errors["phone"] = str(exc)
            phone = None
        role = str(data.get("role") or Role.TEACHER)
        if role not in (Role.TEACHER, *SUPPORT_ROLES):
            errors["role"] = "Choose teacher, office, accounts, transport, driver or attendant."
        subject = None
        if data.get("subject_id"):
            try:
                subject = Subject.objects.filter(id=uuid.UUID(str(data["subject_id"]))).first()
            except ValueError:
                subject = None
            if subject is None:
                errors["subject_id"] = "Choose a subject."
        joined = None
        if data.get("joined_on"):
            try:
                joined = date.fromisoformat(str(data["joined_on"]))
            except ValueError:
                errors["joined_on"] = "Use a date like 2026-10-01."
        department = str(data.get("department") or "")
        if department and department not in Department.values:
            errors["department"] = "Choose a department."
        if phone and Membership.objects.filter(user__phone=phone, role=role).exists():
            errors["phone"] = "Someone with this number already has this role here."
        if errors:
            raise ValidationError(errors)
        title = str(data.get("title") or (subject.name if subject else "")).strip()[:80]
        with transaction.atomic():
            user = User.objects.filter(phone=phone).first() or User.objects.create_user(phone, name)
            Membership.objects.create(user=user, role=role, title=title, department=department)
            existing = StaffProfile.objects.filter(user=user).first()
            prefix = "SPS-T" if role == Role.TEACHER else "SPS-S"
            numbers = [int(e.rsplit("-", 1)[1]) for e in StaffProfile.objects.filter(employee_id__startswith=prefix).values_list("employee_id", flat=True) if e.rsplit("-", 1)[-1].isdigit()]
            employee_id = str(data.get("employee_id") or "").strip() or f"{prefix}-{(max(numbers) if numbers else 0) + 1:04d}"
            if existing is None:
                StaffProfile.objects.create(user=user, employee_id=employee_id[:30], designation=title, joined_on=joined or school_today(request.school))
        audit(request, "staff.create", target=user, module="staff", summary=f"{name} added as {Role(role).label}", detail={"role": role, "title": title})
        return Response({"id": str(user.id), "employee_id": employee_id}, status=201)


class StaffExportView(SchoolAPIView):
    """CSV of the tab's staff with today's status and term attendance. Audited."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        today = school_today(request.school)
        tab = request.query_params.get("tab") or "teaching"
        if tab not in ("teaching", "support"):
            raise ValidationError({"tab": "Use teaching or support."})
        rows = _filter(directory(request, staff_members(tab), today), request.query_params)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Employee ID", "Name", "Subject / role", "Classes", "Attendance % (term)", "Periods / week", "Today"])
        for r in rows:
            classes = ", ".join(r["classes"]["labels"]) if r["classes"] else ""
            writer.writerow([r["employee_id"] or "", r["name"], r["subject"] or "", classes, r["attendance"] if r["attendance"] is not None else "", r["periods"] if r["periods"] is not None else "", r["today"]["state"]])
        audit(request, "staff.export", target=("staff", tab), summary=f"Exported {len(rows)} {tab} staff", detail={"count": len(rows), "tab": tab})
        response = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="staff-{tab}-{today.isoformat()}.csv"'
        return response


class StaffLeaveDecideView(SchoolAPIView):
    """Approve or decline a leave request through the approvals engine. Audited."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, request_id):
        from apps.approvals.services import decide, payload

        try:
            req = ApprovalRequest.objects.filter(id=uuid.UUID(str(request_id)), kind="leave").first()
        except ValueError:
            req = None
        if req is None:
            raise Http404
        decision = str(request.data.get("decision") or "")
        if decision not in ("approve", "decline"):
            raise ValidationError({"decision": "Use approve or decline."})
        note = str(request.data.get("note") or "")
        if decision == "decline" and not note.strip():
            raise ValidationError({"note": "Say why, so they know."})
        req = decide(req, decision, request.user, note=note, device=(request.META.get("HTTP_USER_AGENT") or "")[:200])
        audit(request, f"approval.{decision}", target=req, module="staff", summary=f"Leave · {req.summary}")
        return Response(payload(req))


urlpatterns = [
    path("console/staff", StaffView.as_view()),
    path("console/staff/export", StaffExportView.as_view()),
    path("console/staff/leave/<str:request_id>/decide", StaffLeaveDecideView.as_view()),
]
