"""Generating reports (library, custom, the analytics summary), schedules and their delivery."""

from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db.models import Sum
from django.utils import timezone

from apps.academics.models import Student
from apps.core.utils import school_now, school_today, school_tz

from . import library
from .library import Filters, Table
from .models import CustomReport, ReportDelivery, ReportRun, ScheduledReport

CONTENT_TYPES = {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


# ------------------------------------------------------------------ filters


def terms(school) -> list[dict]:
    return sorted((school.settings or {}).get("terms") or [], key=lambda t: t.get("starts_on", ""))


def default_filters(school, term_name: str | None = None) -> Filters:
    """The current term, from the first month with a register (schools reopen in June) up to today."""
    from apps.attendance.models import AttendanceSession

    today = school_today(school)
    ts = terms(school)
    term = next((t for t in ts if t["name"] == term_name), None) or next((t for t in ts if t["starts_on"] <= today.isoformat() <= t["ends_on"]), None)
    if term is None:
        return Filters(start=today.replace(day=1), end=today)
    start, end = date.fromisoformat(term["starts_on"]), min(date.fromisoformat(term["ends_on"]), today)
    first = AttendanceSession.objects.filter(date__gte=start, date__lte=end).order_by("date").values_list("date", flat=True).first()
    if first:
        start = first.replace(day=1)
    return Filters(start=start, end=max(start, end), term=term["name"])


def filters_from(school, params) -> Filters:
    f = default_filters(school, params.get("term") or None)
    try:
        if params.get("from"):
            f.start = date.fromisoformat(params["from"])
        if params.get("to"):
            f.end = date.fromisoformat(params["to"])
    except ValueError:
        pass
    if f.end < f.start:
        f.start, f.end = f.end, f.start
    raw_grades = params.getlist("grades") if hasattr(params, "getlist") else (params.get("grades") or [])
    raw_sections = params.getlist("sections") if hasattr(params, "getlist") else (params.get("sections") or [])
    f.grades = [g for x in raw_grades for g in str(x).split(",") if g]
    f.sections = [s for x in raw_sections for s in str(x).split(",") if s]
    return f


# ------------------------------------------------------------------ analytics (the three charts)


def _months(start: date, end: date) -> list[date]:
    out, d = [], start.replace(day=1)
    while d <= end:
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def attendance_trend(f: Filters) -> dict:
    from apps.attendance.models import AttendanceException, AttendanceSession
    from apps.staff.models import StaffAttendance

    groups = list(f.groups().values_list("id", flat=True))
    sizes = Counter(Student.objects.filter(is_active=True, class_group_id__in=groups).values_list("class_group_id", flat=True))
    roll, off = Counter(), Counter()
    for gid, d in AttendanceSession.objects.filter(class_group_id__in=groups, date__gte=f.start, date__lte=f.end).values_list("class_group_id", "date"):
        roll[(d.year, d.month)] += sizes.get(gid, 0)
    for d in AttendanceException.objects.filter(session__class_group_id__in=groups, session__date__gte=f.start, session__date__lte=f.end, status__in=["absent", "excused"]).values_list("session__date", flat=True):
        off[(d.year, d.month)] += 1
    staff_rows = defaultdict(Counter)
    for d, st in StaffAttendance.objects.filter(date__gte=f.start, date__lte=f.end).values_list("date", "status"):
        staff_rows[(d.year, d.month)][st] += 1
    months = []
    for m in _months(f.start, f.end):
        k = (m.year, m.month)
        s = staff_rows.get(k, Counter())
        staff_total = sum(s.values())
        months.append(
            {
                "month": m.isoformat(),
                "to_date": (m.year, m.month) == (f.end.year, f.end.month) and f.end.day < monthrange(m.year, m.month)[1],
                "students": round((roll[k] - off[k]) * 100 / roll[k], 1) if roll[k] else None,
                "staff": round((s["present"] + s["late"]) * 100 / staff_total, 1) if staff_total else None,
            }
        )
    return {"months": months}


def _exam_pct(qs) -> float | None:
    row = qs.aggregate(m=Sum("marks"), o=Sum("max_marks"))
    return round(float(row["m"]) * 100 / float(row["o"]), 1) if row["m"] is not None and row["o"] else None


def unit_tests(f: Filters) -> dict:
    """UT1 vs UT2 average by grade, the school averages, and the biggest drop (grade and subject)."""
    from apps.principal.views import _grade_key
    from apps.results.models import ExamMark

    base = ExamMark.objects.filter(exam__class_group__in=f.groups(), exam__is_published=True, is_absent=False)
    names = ("Unit Test 1", "Unit Test 2")
    rows = defaultdict(dict)
    for r in base.filter(exam__name__in=names).values("exam__name", "exam__class_group__grade").annotate(m=Sum("marks"), o=Sum("max_marks")):
        rows[r["exam__class_group__grade"]][r["exam__name"]] = round(float(r["m"]) * 100 / float(r["o"]), 1) if r["o"] else None
    grades = sorted((g for g in rows if g.isdigit()), key=_grade_key)
    out = [{"grade": g, "ut1": rows[g].get(names[0]), "ut2": rows[g].get(names[1])} for g in grades]
    diffs = [(r["ut2"] - r["ut1"], r["grade"]) for r in out if r["ut1"] is not None and r["ut2"] is not None]
    worst = None
    if diffs and min(diffs)[0] < 0:
        drop, grade = min(diffs)
        subjects = defaultdict(dict)
        for r in base.filter(exam__name__in=names, exam__class_group__grade=grade).values("exam__name", "subject__name").annotate(m=Sum("marks"), o=Sum("max_marks")):
            subjects[r["subject__name"]][r["exam__name"]] = float(r["m"]) * 100 / float(r["o"]) if r["o"] else None
        by_subject = [(v[names[1]] - v[names[0]], s) for s, v in subjects.items() if v.get(names[0]) is not None and v.get(names[1]) is not None]
        subject = min(by_subject) if by_subject else None
        worst = {"grade": grade, "drop": round(drop, 1), "subject": subject[1] if subject else None, "subject_drop": round(subject[0], 1) if subject else None}
    return {
        "grades": out,
        "school": {"ut1": _exam_pct(base.filter(exam__name=names[0])), "ut2": _exam_pct(base.filter(exam__name=names[1]))},
        "improved": sum(1 for d, _g in diffs if d > 0),
        "compared": len(diffs),
        "worst": worst,
    }


def fee_collection(school, f: Filters) -> dict:
    """Cumulative collection by month for the term, against the target (a share of what was billed)."""
    from apps.fees.models import FeeInvoice, Payment

    ts = terms(school)
    term = next((t for t in ts if t["name"] == f.term), None)
    start = date.fromisoformat(term["starts_on"]) if term else f.start
    end = min(date.fromisoformat(term["ends_on"]), f.end) if term else f.end
    invoices = FeeInvoice.objects.filter(student__class_group__in=f.groups())
    if term:
        invoices = invoices.filter(due_date__gte=term["starts_on"], due_date__lte=term["ends_on"])
    billed = invoices.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    target_pct = (((school.settings or {}).get("fees") or {}).get("target") or {}).get("percent") or 90
    by_month = Counter()
    for paid_at, amount in Payment.objects.filter(invoice__in=invoices, status=Payment.Status.SUCCEEDED, paid_at__date__lte=end).values_list("paid_at", "amount"):
        d = timezone.localtime(paid_at, school_tz(school)).date()
        by_month[(max(d, start).year, max(d, start).month)] += float(amount)
    running, months = 0.0, []
    for m in _months(start, end):
        running += by_month[(m.year, m.month)]
        months.append({"month": m.isoformat(), "collected": round(running)})
    target = float(billed) * target_pct / 100
    return {"months": months, "billed": float(billed), "target": round(target), "target_percent": target_pct, "collected": round(running), "gap": round(max(0, target - running))}


def analytics(school, f: Filters) -> dict:
    return {"attendance": attendance_trend(f), "unit_tests": unit_tests(f), "fees": fee_collection(school, f)}


def summary_table(school, f: Filters) -> Table:
    """The charts as one sheet (the page's Export)."""
    data = analytics(school, f)
    rows = [["Attendance", m["month"][:7], "Students % present", m["students"]] for m in data["attendance"]["months"]]
    rows += [["Attendance", m["month"][:7], "Staff % present", m["staff"]] for m in data["attendance"]["months"]]
    rows += [["Unit tests", f"Grade {g['grade']}", "Unit Test 1 %", g["ut1"]] for g in data["unit_tests"]["grades"]]
    rows += [["Unit tests", f"Grade {g['grade']}", "Unit Test 2 %", g["ut2"]] for g in data["unit_tests"]["grades"]]
    rows += [["Fees", m["month"][:7], "Collected to date (₹)", m["collected"]] for m in data["fees"]["months"]]
    rows += [["Fees", "Term", "Target (₹)", data["fees"]["target"]], ["Fees", "Term", "Billed (₹)", data["fees"]["billed"]]]
    return Table("Analytics summary", ["Chart", "Period / group", "Measure", "Value"], rows)


# ------------------------------------------------------------------ generating


def build(report: str, f: Filters, school) -> tuple[Table, list[str]]:
    if report == "summary":
        return summary_table(school, f), ["xlsx"]
    if report.startswith("custom:"):
        c = CustomReport.objects.filter(id=report.split(":", 1)[1]).first()
        if c is None:
            raise KeyError(report)
        return library.custom(c.module, c.columns, f, c.name), ["pdf", "xlsx"]
    spec = library.LIBRARY[report]
    return spec["build"](f), spec["formats"]


def generate(school, report: str, fmt: str, f: Filters, user=None, schedule=None) -> ReportRun:
    table, formats = build(report, f, school)
    if fmt not in formats:
        raise ValueError(f"{report} comes as {', '.join(formats)}")
    data = library.render(table, fmt, school.name, f)
    stamp = school_now(school).strftime("%Y-%m-%d_%H%M")
    run = ReportRun(
        report=report,
        custom_id=report.split(":", 1)[1] if report.startswith("custom:") else None,
        title=table.title,
        format=fmt,
        size=len(data),
        rows=len(table.rows),
        filters=f.as_dict(),
        generated_by=user,
        schedule=schedule,
    )
    run.file.save(f"{table.title.replace(' ', '_')}_{stamp}.{fmt}", ContentFile(data), save=False)
    run.save()
    return run


# ------------------------------------------------------------------ schedules


def next_run(s: ScheduledReport, after: datetime) -> datetime:
    """The first scheduled time strictly after ``after`` (in the school's time zone). Sundays are skipped for daily runs."""
    tz = school_tz(s.school)
    local = after.astimezone(tz)
    day = local.date()
    for _ in range(400):
        candidate = datetime.combine(day, s.at, tzinfo=tz)
        ok = (
            (s.frequency == ScheduledReport.Frequency.DAILY and day.weekday() != 6)
            or (s.frequency == ScheduledReport.Frequency.WEEKLY and day.weekday() == s.weekday)
            or (s.frequency == ScheduledReport.Frequency.MONTHLY and day.day == s.day_of_month)
        )
        if ok and candidate > local:
            return candidate
        day += timedelta(days=1)
    return datetime.combine(day, s.at, tzinfo=tz)


def run_schedule(s: ScheduledReport, now: datetime | None = None) -> ReportRun:
    """Generate one scheduled report and deliver it in-app (and by email where the person has an address)."""
    from apps.notifications.channels import provider_for
    from apps.notifications.models import Category
    from apps.notifications.services import notify

    now = now or timezone.now()
    school = s.school
    f = default_filters(school)
    run = generate(school, s.report, s.format, f, schedule=s)
    recipients = list(s.recipients.filter(is_active=True))
    notify(
        recipients,
        school=school,
        category=Category.GENERAL,
        title=f"{s.name} is ready",
        body=f"{run.title} · {run.rows} rows · {run.format.upper()}",
        data={"type": "report", "report_run_id": str(run.id), "download": f"/console/reports/runs/{run.id}/file"},
        dedupe_key=f"report-run:{run.id}",
        push=False,
    )
    rows = [ReportDelivery(school=school, schedule=s, run=run, user=u, channel="in_app", status="delivered") for u in recipients]
    email = provider_for("email")
    for u in recipients:
        if u.email:
            email.send("email", u.email, f"{s.name}\n\n{run.title} · {f.describe()}\nDownload: /console/reports/runs/{run.id}/file")
            rows.append(ReportDelivery(school=school, schedule=s, run=run, user=u, channel="email", address=u.email, status=getattr(email, "delivered_status", "sent")))
    ReportDelivery.objects.bulk_create(rows)
    s.last_run_at = now
    s.next_run_at = next_run(s, now)
    s.save(update_fields=["last_run_at", "next_run_at", "updated_at"])
    return run


def run_due(now: datetime | None = None) -> int:
    now = now or timezone.now()
    n = 0
    for s in ScheduledReport.objects.filter(enabled=True, next_run_at__lte=now).select_related("school"):
        run_schedule(s, now)
        n += 1
    return n

