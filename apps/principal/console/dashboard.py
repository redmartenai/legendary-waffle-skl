"""Console: the dashboard. The school today in one screen: briefing, register heat grid, cover, UT trend,
in-tray, buses and fee collection."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Avg, Min, Sum
from django.urls import path
from django.utils import timezone
from rest_framework.response import Response

from apps.academics.models import ClassGroup, StudentGuardian
from apps.attendance.models import AttendanceException, AttendanceSession
from apps.core.api import SchoolAPIView
from apps.core.utils import school_now, school_today, school_tz

from ..views import GRADE_ORDER, _grade_key, _pct, _school_days_back, chronic_absentees, cover_board, register_today
from .common import CONSOLE_ROLES, current_term, next_term


def _section_history(section_id, size: int, days: list[date]) -> list[tuple[date, float]]:
    """% present for one section on each of ``days`` that has a register."""
    missing = Counter(
        AttendanceException.objects.filter(session__class_group_id=section_id, session__date__in=days, status__in=["absent", "excused"]).values_list(
            "session__date", flat=True
        )
    )
    marked = AttendanceSession.objects.filter(class_group_id=section_id, date__in=days).values_list("date", flat=True)
    return sorted((d, _pct(size - missing.get(d, 0), size)) for d in marked)


def register_grid(school, today: date) -> dict:
    """Every section's % present today, laid out grade × section letter, plus the "call today" note."""
    reg = register_today(school, today)
    sections = [s for s in reg["by_section"]]
    letters = sorted({s["label"].split("-")[-1] for s in sections})
    grades = sorted({s["grade"] for s in sections}, key=_grade_key)
    cells = {}
    for s in sections:
        pct = _pct(s["present"] + s["late"], s["total"]) if s["marked"] else None
        cells[f'{s["grade"]}|{s["label"].split("-")[-1]}'] = {"id": s["id"], "label": s["label"], "percent": pct, "marked": s["marked"]}
    marked = [s for s in sections if s["marked"]]
    # Sections below 90% today, worst first; the worst one is compared with its best day in the last fortnight.
    low = sorted(
        ({"id": s["id"], "label": s["label"], "total": s["total"], "percent": _pct(s["present"] + s["late"], s["total"])} for s in marked),
        key=lambda s: s["percent"],
    )
    low = [s for s in low if s["percent"] < 90]
    call = None
    if low:
        worst = low[0]
        history = _section_history(worst["id"], worst["total"], _school_days_back(today, 10))
        best = max(history, key=lambda h: (h[1], h[0])) if history else None
        chronic = chronic_absentees(today)
        low_labels = {s["label"] for s in low[:2]}
        call = {
            "section": worst["label"],
            "percent": worst["percent"],
            "from_percent": best[1] if best else None,
            "from_date": best[0].isoformat() if best else None,
            "also": [{"label": s["label"], "percent": s["percent"]} for s in low[1:2]],
            "chronic_in_sections": sum(1 for c in chronic if c["class"] in low_labels),
            "chronic": len(chronic),
        }
    counts = Counter()
    for s in marked:
        p = _pct(s["present"] + s["late"], s["total"])
        counts["97" if p >= 97 else "94" if p >= 94 else "90" if p >= 90 else "low"] += 1
    return {
        "grades": grades,
        "letters": letters,
        "cells": cells,
        "sections": reg["sections"],
        "sections_marked": reg["sections_marked"],
        "last_marked_at": reg["last_marked_at"],
        "present": reg["present"] + reg["late"],
        "total": reg["total"],
        "legend": counts,
        "call": call,
    }


def cover_summary(school, today: date) -> dict:
    board = cover_board(school, today)
    periods = []
    open_rows = []
    placed = []
    for p in board["periods"]:
        slots = p["slots"]
        covered = sum(1 for s in slots if s["covered_by"])
        periods.append({"period": p["period"], "state": p["state"], "slots": len(slots), "open": p["open"], "covered": covered, "starts_at": p["starts_at"], "ends_at": p["ends_at"]})
        if covered:
            placed.append(p["period"])
        if p["open"] and p["state"] != "done":
            open_rows.append(
                {
                    "period": p["period"],
                    "starts_at": p["starts_at"],
                    "now": p["state"] == "now",
                    "classes": [f'{s["class"]} {s["subject"]}' for s in slots if not s["covered_by"]],
                    "free": [f["name"] for f in p["free"][:3]],
                }
            )
    return {
        "on_leave": [t["name"] for t in board["on_leave"]],
        "open": sum(p["open"] for p in board["periods"] if p["state"] != "done"),
        "missed": sum(p["open"] for p in board["periods"] if p["state"] == "done"),
        "periods": periods,
        "open_rows": open_rows,
        "placed": placed,
    }


def exam_trend(school) -> dict | None:
    """Average % by grade for the last two published exams, and the subject that moved most in the weakest grade."""
    from apps.results.models import Exam, ExamMark

    names = list(
        Exam.objects.filter(is_published=True).values("name").annotate(first=Min("held_on")).order_by("first").values_list("name", flat=True)
    )
    if len(names) < 2:
        return None
    prev, last = names[-2], names[-1]
    # One pass: (exam, grade, subject) → marks and max marks; everything else is summed from it.
    rows = (
        ExamMark.objects.filter(exam__name__in=[prev, last], exam__is_published=True, is_absent=False)
        .values("exam__name", "exam__class_group__grade", "subject_id", "subject__name")
        .annotate(m=Sum("marks"), o=Sum("max_marks"))
    )
    got = defaultdict(lambda: [0.0, 0.0])
    subject_names = {}
    for r in rows:
        key = (r["exam__name"], r["exam__class_group__grade"], r["subject_id"])
        got[key][0] += float(r["m"] or 0)
        got[key][1] += float(r["o"] or 0)
        subject_names[r["subject_id"]] = r["subject__name"]

    def pct(name, grade=None, subject=None):
        m = o = 0.0
        for (n, g, sid), (a, b) in got.items():
            if n == name and (grade is None or g == grade) and (subject is None or sid == subject):
                m, o = m + a, o + b
        return round(m * 100 / o, 1) if o else None

    grades = sorted({g for (_n, g, _s) in got if g.isdigit()}, key=int)
    trend = []
    for g in grades:
        a, b = pct(prev, g), pct(last, g)
        if a is not None and b is not None:
            trend.append({"grade": g, "from": a, "to": b})
    if not trend:
        return None
    worst = min(trend, key=lambda r: r["to"] - r["from"])
    best = max(trend, key=lambda r: r["to"] - r["from"])
    subject = None
    if worst["to"] < worst["from"]:
        moves = []
        for sid in {s for (_n, g, s) in got if g == worst["grade"]}:
            a, b = pct(prev, worst["grade"], sid), pct(last, worst["grade"], sid)
            if a is not None and b is not None:
                moves.append((b - a, subject_names[sid]))
        if moves:
            delta, sname = min(moves)
            subject = {"name": sname, "delta": round(delta, 1)}
    return {
        "from_exam": prev,
        "to_exam": last,
        "from_avg": pct(prev),
        "to_avg": pct(last),
        "rows": trend,
        "down": {"grade": worst["grade"], "delta": round(worst["to"] - worst["from"], 1), "subject": subject} if worst["to"] < worst["from"] else None,
        "up": {"grade": best["grade"], "delta": round(best["to"] - best["from"], 1)} if best["to"] > best["from"] else None,
    }


def buses_now(school) -> dict:
    """Every drop run today, late first: progress along its stops and a status."""
    from apps.transport import services as transport
    from apps.transport.models import Direction, Trip, Vehicle

    transport.ensure_today_trips(school)
    today = school_today(school)
    now = school_now(school)
    tz = school_tz(school)
    rows = []
    trips = (
        Trip.objects.filter(service_date=today, direction=Direction.DROP)
        .select_related("route", "vehicle", "school", "driver", "attendant")
        .prefetch_related("route__stops")
    )
    for trip in trips:
        live = transport.live_state(trip, staff=True)
        stops = live.get("stops") or []
        delay = live.get("delay_minutes") or 0
        start = datetime.combine(today, trip.scheduled_start, tzinfo=tz)
        if trip.status == Trip.Status.SCHEDULED and start < now <= start + timedelta(hours=1):
            delay = max(delay, int((now - start).total_seconds() // 60))
        done = sum(1 for s in stops if s["status"] in ("departed", "skipped"))
        at = next((i for i, s in enumerate(stops) if s["status"] == "at_stop"), None)
        position = at if at is not None else max(0, done - 1) + (0.5 if trip.status == Trip.Status.ACTIVE and done else 0)
        state = (
            "done"
            if trip.status in (Trip.Status.COMPLETED,)
            else "late"
            if delay >= 5
            else "moving"
            if trip.status == Trip.Status.ACTIVE
            else "waiting"
        )
        rows.append(
            {
                "route_id": str(trip.route_id),
                "route": trip.route.name,
                "state": state,
                "delay_minutes": delay,
                "leaves_at": (start + timedelta(minutes=delay)).strftime("%H:%M"),
                "stops": len(stops),
                "position": position,
                "passed": done,
            }
        )
    rows.sort(key=lambda r: (-r["delay_minutes"], r["route"]))
    return {
        "out": sum(1 for r in rows if r["state"] in ("moving", "late") and r["passed"] > 0) or sum(1 for r in rows if r["state"] != "done"),
        "total": Vehicle.objects.filter(is_active=True).count(),
        "routes": rows,
    }


def fee_collection(school, today: date) -> dict:
    """This term's collection, from the Fees page's own helper so both pages agree."""
    from .fees import fee_collection as term_collection

    term = current_term(school, today)
    data = term_collection(school, today)
    target = ((school.settings or {}).get("fees") or {}).get("target") or {"percent": 90, "by": term["ends_on"] if term else None}
    return {
        "term": term["name"] if term else None,
        "billed": data["billed"],
        "collected": data["collected"],
        "overdue": data["overdue"]["amount"],
        "overdue_students": data["overdue"]["students"],
        "overdue_families": data["overdue"]["families"],
        "months": [{"month": m["month"], "amount": m["amount"]} for m in data["months"] if not m.get("future")],
        "target": target,
        "current_month": f"{today.year}-{today.month:02d}",
    }


def coming_up(school, today: date) -> list[dict]:
    """The next few dated things: fee due dates, events (PTM), and exam series."""
    from apps.announcements.models import Announcement
    from apps.fees.models import FeeInvoice
    from apps.messaging.models import Meeting
    from apps.results.models import ExamPaper

    tz = school_tz(school)
    items = []
    due = FeeInvoice.objects.filter(due_date__gt=today).order_by("due_date").values_list("due_date", flat=True).first()
    if due:
        term = current_term(school, today)
        upcoming = next_term(school, today)
        overdue = sum(
            (i.amount - i.paid_amount for i in FeeInvoice.objects.filter(due_date__lte=today).only("amount", "paid_amount") if i.amount > i.paid_amount),
            Decimal("0"),
        )
        items.append(
            {
                "kind": "fees",
                "date": due.isoformat(),
                "title_term": (upcoming or {}).get("name") if upcoming and due.isoformat() >= upcoming.get("starts_on", "9") else (term or {}).get("name"),
                "outstanding": str(overdue),
                "outstanding_term": (term or {}).get("name"),
            }
        )
    for a in Announcement.objects.filter(kind__in=["event", "holiday"], event_starts_at__gt=timezone.now()).order_by("event_starts_at")[:3]:
        start = timezone.localtime(a.event_starts_at, tz)
        end = timezone.localtime(a.event_ends_at, tz) if a.event_ends_at else None
        booked = Meeting.objects.filter(starts_at__date=start.date(), status=Meeting.Status.BOOKED).count()
        items.append(
            {
                "kind": a.kind,
                "date": start.date().isoformat(),
                "title": a.title.split(" · ")[0],
                "starts": start.strftime("%H:%M"),
                "ends": end.strftime("%H:%M") if end else None,
                "booked": booked,
                "id": str(a.id),
            }
        )
    series = (
        ExamPaper.objects.filter(date__gt=today, exam__is_published=False)
        .values("exam__name")
        .annotate(first=Min("date"))
        .order_by("first")
        .first()
    )
    if series:
        papers = ExamPaper.objects.filter(exam__name=series["exam__name"], date__gte=series["first"])
        days = papers.values("date").distinct().count()
        items.append({"kind": "exam", "date": series["first"].isoformat(), "title": series["exam__name"], "papers": papers.count(), "days": days})
    items.sort(key=lambda i: i["date"])
    return items[:3]


class DashboardView(SchoolAPIView):
    """Everything the console dashboard shows, in one request."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        from apps.approvals.models import ApprovalRequest
        from apps.approvals.services import payload
        from apps.approvals.views import urgency
        from apps.results.models import ExamPaper

        school = request.school
        today = school_today(school)
        pending = list(ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING).select_related("requested_by"))
        pending.sort(key=urgency(today))
        exam_day = ExamPaper.objects.filter(date__gte=today, exam__is_published=False).order_by("date").values_list("date", "exam__name").first()
        school_days = sum(1 for i in range(1, (exam_day[0] - today).days + 1) if (today + timedelta(days=i)).weekday() != 6) if exam_day else None
        return Response(
            {
                "now": school_now(school).isoformat(),
                "today": today.isoformat(),
                "register": register_grid(school, today),
                "cover": cover_summary(school, today),
                "exams": exam_trend(school),
                "intray": {
                    "total": len(pending),
                    "top": payload(pending[0]) if pending else None,
                    "next": [
                        {"id": str(r.id), "kind": r.kind, "name": r.requested_by.full_name if r.requested_by else "", "summary": r.summary} for r in pending[1:3]
                    ],
                },
                "buses": buses_now(school),
                "fees": fee_collection(school, today),
                "coming_up": coming_up(school, today),
                "next_exam": {"name": exam_day[1], "date": exam_day[0].isoformat(), "school_days": school_days} if exam_day else None,
                "grade_order": GRADE_ORDER,
            }
        )


urlpatterns = [path("console/dashboard", DashboardView.as_view())]
