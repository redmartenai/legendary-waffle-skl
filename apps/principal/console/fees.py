"""Console: Fees & Finance. Collection for a term (or the year), month by month, today's counter, the overdue
ledger grouped by family with reminders and calls, refunds waiting for a decision, grade-by-grade collection and the
published fee structure.

``fee_collection(school, today, period)`` is the reusable summary (billed / collected / overdue / on plan / by month /
overdue families); the dashboard can call it too.
"""

import csv
import io
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Count, Sum
from django.http import Http404, HttpResponse
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import AcademicYear, StudentGuardian
from apps.accounts.audit import audit
from apps.accounts.models import User
from apps.core.api import SchoolAPIView
from apps.core.utils import school_now, school_today, school_tz
from apps.fees.models import FeeInvoice, FeeReminder, FeeStructure, InstalmentPlan, Payment
from apps.notifications.channels import address_for, provider_for
from apps.notifications.models import Category
from apps.notifications.services import notify

from ..views import GRADE_ORDER, PRE_PRIMARY, _grade_key, _pct
from .common import CONSOLE_ROLES, current_term

ONLINE = ("upi", "card", "netbanking")
METHOD_ORDER = ("upi", "card", "netbanking", "cash", "cheque")
UNDER = 75.0  # grades collecting less than this are flagged
CHANNELS = ("app", "sms", "whatsapp")
PREVIEW_ROWS = 6


def money(v) -> str:
    return str(Decimal(v or 0).quantize(Decimal("1")))


# --------------------------------------------------------------------------------------------------- periods


def periods(school, today: date) -> tuple[list[dict], str]:
    """The billing periods the page can show: each term, then the full academic year. Returns (periods, current key)."""
    terms = sorted((school.settings or {}).get("terms") or [], key=lambda t: t.get("starts_on", ""))
    out = [{"key": f"term{i + 1}", "name": t["name"], "starts_on": t["starts_on"], "ends_on": t["ends_on"]} for i, t in enumerate(terms)]
    year = AcademicYear.objects.filter(is_current=True).first()
    if year:
        out.append({"key": "year", "name": "Full year", "starts_on": year.starts_on.isoformat(), "ends_on": year.ends_on.isoformat()})
    elif terms:
        out.append({"key": "year", "name": "Full year", "starts_on": terms[0]["starts_on"], "ends_on": terms[-1]["ends_on"]})
    term = current_term(school, today)
    current = next((p["key"] for p in out if term and p["name"] == term["name"]), out[0]["key"] if out else "year")
    return out, current


def resolve_period(school, today: date, key: str | None) -> dict:
    options, current = periods(school, today)
    key = key or current
    found = next((p for p in options if p["key"] == key), None)
    if found is None:
        raise ValidationError({"period": f"Use one of: {', '.join(p['key'] for p in options)}."})
    return {**found, "current": key == current}


def period_invoices(period: dict):
    return FeeInvoice.objects.filter(due_date__gte=period["starts_on"], due_date__lte=period["ends_on"])


# --------------------------------------------------------------------------------------------------- overdue


def _plan_overdue(inv: FeeInvoice, plan: InstalmentPlan | None, today: date) -> Decimal:
    """What's overdue on one invoice. On an active instalment plan only the missed instalments count."""
    if inv.balance <= 0 or inv.due_date >= today:
        return Decimal("0")
    if plan is None or not plan.is_active:
        return inv.balance
    due = sum((Decimal(str(i["amount"])) for i in plan.instalments if i["due_on"] < today.isoformat()), Decimal("0"))
    return max(Decimal("0"), min(inv.balance, due - inv.paid_amount))


def _guardians(student_ids) -> dict:
    """student id → their primary (or first) guardian."""
    out = {}
    for link in StudentGuardian.objects.filter(student_id__in=student_ids).select_related("user").order_by("-is_primary", "created_at"):
        out.setdefault(link.student_id, link.user)
    return out


def overdue_families(school, today: date, period: dict) -> list[dict]:
    """Families (by guardian) with fees past due in the period, largest dues first."""
    invoices = list(period_invoices(period).filter(due_date__lt=today).select_related("student__class_group"))
    invoices = [i for i in invoices if i.amount > i.paid_amount]
    plans = {p.invoice_id: p for p in InstalmentPlan.objects.filter(invoice_id__in=[i.id for i in invoices])}
    owing = []
    for inv in invoices:
        amount = _plan_overdue(inv, plans.get(inv.id), today)
        if amount > 0:
            owing.append((inv, amount))
    guardians = _guardians({inv.student_id for inv, _a in owing})
    families: dict[str, dict] = {}
    for inv, amount in owing:
        g = guardians.get(inv.student_id)
        key = str(g.id) if g else f"student:{inv.student_id}"
        fam = families.setdefault(key, {"guardian": g, "students": {}, "amount": Decimal("0"), "oldest": inv.due_date})
        fam["students"][inv.student_id] = inv.student
        fam["amount"] += amount
        fam["oldest"] = min(fam["oldest"], inv.due_date)
    last = {}
    ids = [f["guardian"].id for f in families.values() if f["guardian"]]
    for r in FeeReminder.objects.filter(guardian_id__in=ids).order_by("guardian_id", "-sent_at"):
        last.setdefault(r.guardian_id, r)
    rows = []
    for key, f in families.items():
        g = f["guardian"]
        kids = sorted(f["students"].values(), key=lambda s: (_grade_key(s.class_group.grade), s.class_group.section))
        rows.append({
            "id": key,
            "guardian": {"id": str(g.id), "name": g.full_name, "phone": g.phone} if g else None,
            "children": [{"id": str(s.id), "name": s.full_name, "first_name": s.full_name.split()[0], "class": s.class_group.short_label} for s in kids],
            "amount": money(f["amount"]),
            "overdue_days": (today - f["oldest"]).days,
            "oldest_due": f["oldest"].isoformat(),
            "last_reminder": reminder_payload(last[g.id]) if g and g.id in last else None,
        })
    rows.sort(key=lambda r: (-Decimal(r["amount"]), -r["overdue_days"], r["guardian"]["name"] if r["guardian"] else ""))
    return rows


def reminder_payload(r: FeeReminder) -> dict:
    return {"channels": r.channels, "sent_at": r.sent_at.isoformat(), "status": r.status, "note": r.note}


# --------------------------------------------------------------------------------------------------- collection


def fee_collection(school, today: date, period: str | dict | None = None) -> dict:
    """Billed, collected, overdue and on-plan amounts for a period (default: the current term), with the
    collection by month (from succeeded payments' ``paid_at``) and the overdue family/student counts."""
    p = period if isinstance(period, dict) else resolve_period(school, today, period)
    invoices = period_invoices(p)
    totals = invoices.aggregate(b=Sum("amount"), c=Sum("paid_amount"))
    billed, collected = totals["b"] or Decimal("0"), totals["c"] or Decimal("0")
    tz = school_tz(school)
    by_month = defaultdict(Decimal)
    methods = defaultdict(Decimal)
    for amount, paid_at, method in Payment.objects.filter(status=Payment.Status.SUCCEEDED, invoice__in=invoices).values_list("amount", "paid_at", "method"):
        local = paid_at.astimezone(tz)
        by_month[(local.year, local.month)] += amount
        methods[method or "other"] += amount
    start = date.fromisoformat(p["starts_on"])
    end = date.fromisoformat(p["ends_on"])
    first = min([(start.year, start.month)] + list(by_month))
    months = []
    y, m = first
    while (y, m) <= (end.year, end.month):
        this = (y, m) == (today.year, today.month)
        future = (y, m) > (today.year, today.month)
        months.append({"month": f"{y}-{m:02d}", "amount": money(by_month.get((y, m))), "open": this, "future": future})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    families = overdue_families(school, today, p)
    plans = InstalmentPlan.objects.filter(invoice__in=invoices, is_active=True).select_related("invoice")
    on_plan = sum((pl.invoice.balance - _plan_overdue(pl.invoice, pl, today) for pl in plans), Decimal("0"))
    overdue = sum((Decimal(f["amount"]) for f in families), Decimal("0"))
    return {
        "period": p,
        "billed": money(billed),
        "collected": money(collected),
        "percent": _pct(float(collected), float(billed)),
        "overdue": {
            "amount": money(overdue),
            "students": sum(len(f["children"]) for f in families),
            "families": len(families),
            "oldest_days": max((f["overdue_days"] for f in families), default=0),
        },
        "on_plan": money(on_plan),
        "months": months,
        "methods": {k: money(v) for k, v in methods.items()},
        "families": families,
    }


def _grades(invoices, families) -> dict:
    by = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    for grade, amount, paid in invoices.values_list("student__class_group__grade", "amount", "paid_amount"):
        key = "pre" if grade in PRE_PRIMARY else grade
        by[key][0] += amount
        by[key][1] += paid
    rows = []
    for key in ["pre"] + [g for g in GRADE_ORDER if g not in PRE_PRIMARY]:
        if key in by:
            b, c = by[key]
            rows.append({"key": key, "billed": money(b), "collected": money(c), "percent": _pct(float(c), float(b))})
    under = [r["key"] for r in rows if r["percent"] is not None and r["percent"] < UNDER]
    overdue_by_grade = Counter()
    for f in families:
        for c in f["children"]:
            grade = c["class"].split("-")[0]
            overdue_by_grade["pre" if grade in PRE_PRIMARY else grade] += 1
    return {
        "rows": rows,
        "threshold": UNDER,
        "under": under,
        "under_overdue_students": sum(overdue_by_grade[g] for g in under),
    }


def _structure(today: date) -> dict | None:
    rows = list(FeeStructure.objects.all())
    if not rows:
        return None
    heads = [h for h in ("tuition", "transport", "activity") if any(r.head == h for r in rows)]
    bands = []
    for r in rows:
        band = next((b for b in bands if b["band"] == r.band), None)
        if band is None:
            band = {"band": r.band, "grades": r.grades, "amounts": {}}
            bands.append(band)
        band["amounts"][r.head] = money(r.annual_amount)
    schedule = []
    for h in heads:
        sample = next(r for r in rows if r.head == h)
        schedule.append({"head": h, "dates": sample.due_dates, "optional": sample.optional})
    return {"academic_year": rows[0].academic_year, "heads": heads, "rows": bands, "schedule": schedule, "locked": True}


def _refunds(school) -> list[dict]:
    from apps.approvals.models import ApprovalRequest
    from apps.approvals.sla import sla_state

    now = timezone.now()
    out = []
    for req in ApprovalRequest.objects.filter(kind=ApprovalRequest.Kind.REFUND, status=ApprovalRequest.Status.PENDING).order_by("created_at"):
        refund = req.target
        if refund is None:
            continue
        student = refund.payment.invoice.student
        out.append({
            "approval_id": str(req.id),
            "amount": money(refund.amount),
            "fee_head": refund.fee_head or refund.payment.invoice.title,
            "student": student.full_name,
            "class": student.class_group.short_label,
            "reason": refund.reason,
            "sla": sla_state(req, now),
        })
    return out


def _receipt(p: Payment, tz) -> dict:
    student = p.invoice.student
    return {
        "id": str(p.id),
        "receipt_no": p.receipt_no,
        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
        "student": {"id": str(student.id), "name": student.full_name, "class": student.class_group.short_label},
        "title": p.invoice.title,
        "method": p.method or p.gateway,
        "amount": money(p.amount),
        "paid_by": p.paid_by.full_name if p.paid_by else None,
        "pdf": f"/fees/payments/{p.id}/receipt.pdf" if p.receipt_no else None,
    }


def _todays_receipts(school, today: date):
    return (
        Payment.objects.filter(status=Payment.Status.SUCCEEDED, paid_at__date=today)
        .select_related("invoice__student__class_group", "paid_by")
        .order_by("-paid_at")
    )


def _next_due(today: date) -> dict | None:
    """The next date a large share of the school's fees falls due (the tear-off calendar)."""
    upcoming = (
        FeeInvoice.objects.filter(due_date__gte=today, category="tuition")
        .values("due_date")
        .annotate(total=Sum("amount"))
        .order_by("due_date")
    )
    for row in upcoming:
        if row["total"] and row["total"] > 0:
            titles = Counter(FeeInvoice.objects.filter(due_date=row["due_date"], category="tuition").values_list("title", flat=True))
            return {"date": row["due_date"].isoformat(), "days": (row["due_date"] - today).days, "title": titles.most_common(1)[0][0]}
    return None


def _last_run() -> str | None:
    """When the last reminder run (a batch to more than one family) went out."""
    batches = FeeReminder.objects.exclude(batch=None).values("batch").annotate(n=Count("id")).filter(n__gt=1).values_list("batch", flat=True)
    latest = FeeReminder.objects.filter(batch__in=list(batches)).order_by("-sent_at").first()
    return latest.sent_at.isoformat() if latest else None


class FeesView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        today = school_today(school)
        tz = school_tz(school)
        options, current = periods(school, today)
        coll = fee_collection(school, today, request.query_params.get("period"))
        p = coll["period"]
        invoices = period_invoices(p)
        families = coll.pop("families")
        target = None
        conf = ((school.settings or {}).get("fees") or {}).get("target")
        if conf and p["current"]:
            amount = Decimal(coll["billed"]) * Decimal(str(conf.get("percent", 90))) / 100
            target = {"percent": conf.get("percent", 90), "by": conf.get("by"), "amount": money(amount), "gap": money(max(Decimal("0"), amount - Decimal(coll["collected"])))}
        # The month with the most payments, and the due date that drove it.
        peak = max(coll["months"], key=lambda m: Decimal(m["amount"]), default=None)
        peak_due = None
        if peak and Decimal(peak["amount"]) > 0:
            y, m = map(int, peak["month"].split("-"))
            due = Counter(invoices.filter(due_date__year=y, due_date__month=m, category="tuition").values_list("due_date", flat=True)).most_common(1)
            peak_due = {"month": peak["month"], "date": due[0][0].isoformat()} if due else None
        receipts = list(_todays_receipts(school, today))
        total_methods = sum((Decimal(v) for v in coll["methods"].values()), Decimal("0"))
        return Response({
            "as_of": school_now(school).isoformat(),
            "academic_year": (AcademicYear.objects.filter(is_current=True).values_list("name", flat=True).first()),
            "periods": [{"key": o["key"], "name": o["name"]} for o in options],
            "current_period": current,
            **coll,
            "target": target,
            "peak_due": peak_due,
            "online_share": _pct(float(sum((Decimal(coll["methods"].get(k, 0)) for k in ONLINE), Decimal("0"))), float(total_methods)),
            "method_shares": [{"method": k, "percent": _pct(float(Decimal(coll["methods"].get(k, 0))), float(total_methods))} for k in METHOD_ORDER if k in coll["methods"]],
            "next_due": _next_due(today),
            "last_run": _last_run(),
            "receipts_today": {
                "count": len(receipts),
                "amount": money(sum((r.amount for r in receipts), Decimal("0"))),
                "items": [_receipt(r, tz) for r in receipts[:4]],
            },
            "refunds": _refunds(school),
            "grades": _grades(invoices, families),
            "structure": _structure(today),
            "ledger": {
                "counts": _segment_counts(families),
                "preview": families[:PREVIEW_ROWS],
            },
        })


def _segment_counts(families) -> dict:
    return {
        "all": len(families),
        "over60": sum(1 for f in families if f["overdue_days"] >= 60),
        "never": sum(1 for f in families if not f["last_reminder"]),
        "students": sum(len(f["children"]) for f in families),
    }


def _filter(families, segment: str, q: str):
    if segment == "over60":
        families = [f for f in families if f["overdue_days"] >= 60]
    elif segment == "never":
        families = [f for f in families if not f["last_reminder"]]
    elif segment not in ("", "all"):
        raise ValidationError({"segment": "Use all, over60 or never."})
    q = q.strip().lower()
    if q:
        families = [
            f for f in families
            if (f["guardian"] and q in f["guardian"]["name"].lower()) or any(q in c["name"].lower() for c in f["children"]) or (f["guardian"] and q.replace(" ", "") in f["guardian"]["phone"])
        ]
    return families


def _int(value, default, lo, hi, field):
    try:
        n = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        raise ValidationError({field: "Must be a number."}) from None
    return max(lo, min(hi, n))


class OverdueView(SchoolAPIView):
    """The overdue ledger, filtered and paged."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        today = school_today(request.school)
        p = resolve_period(request.school, today, request.query_params.get("period"))
        families = overdue_families(request.school, today, p)
        counts = _segment_counts(families)
        rows = _filter(families, request.query_params.get("segment", "all"), request.query_params.get("q", ""))
        page = _int(request.query_params.get("page"), 1, 1, 10_000, "page")
        size = _int(request.query_params.get("page_size"), 25, 1, 200, "page_size")
        return Response({
            "counts": counts,
            "total": len(rows),
            "amount": money(sum((Decimal(f["amount"]) for f in rows), Decimal("0"))),
            "page": page,
            "page_size": size,
            "items": rows[(page - 1) * size : page * size],
        })


def _reminder_text(school, guardian: User, children: list[dict], amount: Decimal, oldest: date) -> tuple[str, str]:
    names = ", ".join(f"{c['first_name']} ({c['class']})" for c in children)
    title = f"Fee reminder · ₹{amount:,.0f} overdue"
    body = f"Fees of ₹{amount:,.0f} for {names} have been due since {oldest.day} {oldest:%b}. Please pay in the app or at the school office. {school.short_name or school.name} accounts."
    return title, body


def send_reminders(request, families: list[dict], channels: list[str]) -> dict:
    """Remind each family on the chosen channels and record a FeeReminder per family (one batch per call)."""
    school = request.school
    batch = uuid.uuid4() if len(families) > 1 else None
    now = timezone.now()
    counts = Counter()
    rows = []
    for f in families:
        if not f["guardian"]:
            counts["skipped"] += 1
            continue
        guardian = User.objects.get(id=f["guardian"]["id"])
        amount = Decimal(f["amount"])
        title, body = _reminder_text(school, guardian, f["children"], amount, date.fromisoformat(f["oldest_due"]))
        deliveries = {}
        if "app" in channels:
            sent = notify([guardian], school=school, category=Category.FEES, title=title, body=body, data={"type": "fees", "student_id": f["children"][0]["id"]})
            deliveries["app"] = {"status": "sent" if sent else "skipped"}
            counts["app"] += 1 if sent else 0
        for channel in ("sms", "whatsapp"):
            if channel not in channels:
                continue
            address = address_for(channel, guardian)
            if not address:
                deliveries[channel] = {"status": "no_address"}
                continue
            provider = provider_for(channel)
            ref = provider.send(channel, address, f"{title}\n{body}")
            deliveries[channel] = {"provider": provider.name, "status": getattr(provider, "delivered_status", "sent"), "ref": ref}
            counts[channel] += 1
        rows.append(FeeReminder(
            school=school, guardian=guardian, student_ids=[c["id"] for c in f["children"]], channels=[c for c in CHANNELS if c in channels],
            amount=amount, status=FeeReminder.Status.SENT, deliveries=deliveries, batch=batch, sent_by=request.user, sent_at=now,
        ))
    FeeReminder.objects.bulk_create(rows)
    return {"families": len(rows), "students": sum(len(r.student_ids) for r in rows), "amount": money(sum((r.amount for r in rows), Decimal("0"))), "channels": dict(counts)}


class RemindersView(SchoolAPIView):
    """Send reminders to the chosen families (``family_ids``) or every family in a segment (``all``)."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        today = school_today(request.school)
        p = resolve_period(request.school, today, request.data.get("period"))
        channels = request.data.get("channels") or list(CHANNELS)
        if not isinstance(channels, list) or not channels or any(c not in CHANNELS for c in channels):
            raise ValidationError({"channels": "Choose at least one of app, sms and whatsapp."})
        families = overdue_families(request.school, today, p)
        if request.data.get("all"):
            chosen = _filter(families, request.data.get("segment", "all"), request.data.get("q", ""))
        else:
            ids = request.data.get("family_ids")
            if not isinstance(ids, list) or not ids:
                raise ValidationError({"family_ids": "Choose at least one family."})
            wanted = {str(i) for i in ids}
            chosen = [f for f in families if f["id"] in wanted]
            if len(chosen) != len(wanted):
                raise ValidationError({"family_ids": "Some of these families no longer have overdue fees."})
        if not chosen:
            raise ValidationError({"family_ids": "No families to remind."})
        result = send_reminders(request, chosen, channels)
        audit(request, "fees.remind", target=("fee_reminder", ""), summary=f"Fee reminders to {result['families']} famil{'y' if result['families'] == 1 else 'ies'} · ₹{Decimal(result['amount']):,.0f} · {', '.join(channels)}", detail={"families": [f["id"] for f in chosen], "channels": channels})
        return Response(result, status=201)


class CallLogView(SchoolAPIView):
    """Log a phone call to a family about overdue fees (it counts as their latest reminder)."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        today = school_today(request.school)
        guardian_id = str(request.data.get("guardian_id") or "")
        status = request.data.get("status")
        note = str(request.data.get("note") or "").strip()
        if status not in ("answered", "no_answer"):
            raise ValidationError({"status": "Use answered or no_answer."})
        if len(note) > 200:
            raise ValidationError({"note": "Keep the note under 200 characters."})
        p = resolve_period(request.school, today, request.data.get("period"))
        fam = next((f for f in overdue_families(request.school, today, p) if f["guardian"] and f["guardian"]["id"] == guardian_id), None)
        if fam is None:
            raise ValidationError({"guardian_id": "This family has no overdue fees."})
        guardian = User.objects.get(id=guardian_id)
        r = FeeReminder.objects.create(
            guardian=guardian, student_ids=[c["id"] for c in fam["children"]], channels=["call"], amount=Decimal(fam["amount"]),
            status=status, note=note, sent_by=request.user, sent_at=timezone.now(),
        )
        audit(request, "fees.call", target=r, summary=f"Call to {guardian.full_name} · {status.replace('_', ' ')}")
        return Response(reminder_payload(r), status=201)


class ExportView(SchoolAPIView):
    """Every invoice in the period as CSV. Audited."""

    allowed_roles = CONSOLE_ROLES
    permission = ("fees", "export")

    def get(self, request):
        today = school_today(request.school)
        p = resolve_period(request.school, today, request.query_params.get("period"))
        invoices = list(period_invoices(p).select_related("student__class_group").order_by("student__class_group__grade", "student__class_group__section", "student__roll_no", "due_date"))
        guardians = _guardians({i.student_id for i in invoices})
        plans = set(InstalmentPlan.objects.filter(invoice__in=invoices, is_active=True).values_list("invoice_id", flat=True))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Admission no", "Student", "Class", "Guardian", "Phone", "Fee", "Due date", "Amount", "Paid", "Balance", "Status", "Instalment plan"])
        for i in invoices:
            g = guardians.get(i.student_id)
            w.writerow([
                i.student.admission_no, i.student.full_name, i.student.class_group.short_label, g.full_name if g else "", g.phone if g else "",
                i.title, i.due_date.isoformat(), money(i.amount), money(i.paid_amount), money(i.balance),
                "on plan" if i.id in plans and i.balance > 0 else i.status_on(today), "yes" if i.id in plans else "",
            ])
        audit(request, "fees.export", target=("fee_invoice", ""), summary=f"Exported {p['name']} fees ({len(invoices)} invoices)", detail={"period": p["key"]})
        name = f"fees-{p['key']}-{today.isoformat()}.csv"
        resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{name}"'
        return resp


class ReceiptsView(SchoolAPIView):
    """Receipts issued on a day (default today), newest first."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        day = request.query_params.get("date")
        try:
            day = date.fromisoformat(day) if day else school_today(school)
        except ValueError:
            raise ValidationError({"date": "Use YYYY-MM-DD."}) from None
        tz = school_tz(school)
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        rows = list(
            Payment.objects.filter(status=Payment.Status.SUCCEEDED, paid_at__gte=start, paid_at__lt=start + timezone.timedelta(days=1))
            .select_related("invoice__student__class_group", "paid_by")
            .order_by("-paid_at")
        )
        return Response({"date": day.isoformat(), "count": len(rows), "amount": money(sum((r.amount for r in rows), Decimal("0"))), "items": [_receipt(r, tz) for r in rows]})


class ReceiptView(SchoolAPIView):
    """One receipt (the global search opens it)."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, payment_id):
        p = Payment.objects.filter(id=payment_id).select_related("invoice__student__class_group", "paid_by").first()
        if p is None or p.status != Payment.Status.SUCCEEDED:
            raise Http404
        inv = p.invoice
        data = _receipt(p, school_tz(request.school))
        data["invoice"] = {"id": str(inv.id), "title": inv.title, "amount": money(inv.amount), "paid_amount": money(inv.paid_amount), "balance": money(inv.balance), "due_date": inv.due_date.isoformat()}
        data["gateway_payment_id"] = p.gateway_payment_id if p.gateway != "counter" else None
        return Response(data)


urlpatterns = [
    path("console/fees", FeesView.as_view()),
    path("console/fees/overdue", OverdueView.as_view()),
    path("console/fees/reminders", RemindersView.as_view()),
    path("console/fees/calls", CallLogView.as_view()),
    path("console/fees/export", ExportView.as_view()),
    path("console/fees/receipts", ReceiptsView.as_view()),
    path("console/fees/receipts/<uuid:payment_id>", ReceiptView.as_view()),
]
