"""Operations seed: fees (PFees).

Makes the fee book look like the design's school: a published fee structure, Term 1 and Term 2 invoices for every
student (tuition, activity, transport for bus riders), payments behind every rupee collected (spread over April to
September with a June peak, UPI/card/net banking/cash/cheque), ~37 receipts today, ~140 overdue students in ~115
families (grades 8–11 the weakest), instalment plans, and a history of reminder runs, WhatsApps and calls.

Idempotent: ``clear`` removes the payments (``gateway_order_id="seed-ops"``), invoices, guardians (phones
5000–5499), plans, reminders and structure rows this part made, and rewinds the receipt counter.
"""

import random
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.academics.models import AcademicYear, Student, StudentGuardian
from apps.accounts.models import Role, User
from apps.fees.models import FeeInvoice, FeeReminder, FeeStructure, InstalmentPlan, Payment, ReceiptCounter
from apps.fees.services import financial_year, next_receipt_no
from apps.transport.models import StudentTransport

MARK = "seed-ops"
PHONES = range(5000, 5500)
Y = 2026

# band, grades, tuition, transport, activity (annual, per student) — the design's fee structure table.
BANDS = [
    ("Pre-primary", ["Nursery", "LKG", "UKG"], 64000, 24000, 6000),
    ("1–5", ["1", "2", "3", "4", "5"], 76000, 24000, 8000),
    ("6–8", ["6", "7", "8"], 85000, 24000, 10000),
    ("9–10", ["9", "10"], 96000, 24000, 12000),
    ("11–12", ["11", "12"], 108000, 24000, 12000),
]
T1_DUE = date(Y, 6, 30)
ACTIVITY_DUE = date(Y, 6, 30)
Q1_DUE, Q2_DUE, Q4_DUE = date(Y, 4, 30), date(Y, 7, 1), date(Y + 1, 1, 1)
MY_TITLES = ("Activity fee 2026–27", "Transport · Q1", "Transport · Q2", "Transport · Q3", "Transport · Q4", "Term 2 tuition")
# Children admitted during the term: their Term 1 fees fall due a month after joining.
LATE_JOIN_DUES = [date(Y, 7, 13), date(Y, 7, 31), date(Y, 8, 6), date(Y, 8, 20)]
# Share of each month's Term 1 payments (the design's April–September bars: 38.2, 21.4, 82.5, 24.1, 11.6, 8.2).
MONTH_WEIGHTS = {4: 38.2, 5: 21.4, 6: 82.5, 7: 24.1, 8: 11.6, 9: 8.2}
METHODS = [("upi", 52), ("card", 9), ("netbanking", 7), ("cash", 25), ("cheque", 7)]
WEAK = ("8", "9", "10", "11")
FATHERS = [
    "Karan", "Imran", "Vivek", "Lionel", "Subhash", "Harsh", "Rajesh", "Anil", "Manoj", "Sanjay", "Pradeep", "Ashok",
    "Naveen", "Girish", "Farhan", "Rakesh", "Deepak", "Mahesh", "Ajay", "Sunil", "Vinod", "Kiran", "Rahim", "Joseph",
]
MOTHERS = ["Priti", "Sneha", "Anjali", "Kavita", "Rekha", "Shabana", "Lata", "Pooja", "Nirmala", "Geeta", "Fatima", "Mary"]
CALL_NOTES = ["Asked for an instalment plan", "Promised to pay by {d}", "No answer; try after 6 PM", "Will pay at the counter on {d}"]


def band_of(grade: str):
    return next(b for b in BANDS if grade in b[1])


def clear(cmd, school):
    FeeReminder.objects.all().delete()
    InstalmentPlan.objects.all().delete()
    FeeStructure.objects.all().delete()
    Payment.objects.filter(gateway_order_id=MARK).delete()
    keep = _family_ids(cmd)
    (
        FeeInvoice.objects.filter(title__in=MY_TITLES, payments__isnull=True)
        .exclude(student_id__in=keep)
        .delete()
    )
    User.objects.filter(phone__in=[cmd._phone(n) for n in PHONES]).delete()
    # Rewind the receipt counter so numbers stay gapless after the payments above are gone.
    for counter in ReceiptCounter.objects.all():
        last = 0
        for no in Payment.objects.filter(receipt_no__contains=f"/{counter.financial_year}/").values_list("receipt_no", flat=True):
            last = max(last, int(no.rsplit("/", 1)[-1]))
        counter.next_number = last + 1
        counter.save(update_fields=["next_number", "updated_at"])


def _family_ids(cmd):
    """Rahul Sharma's children keep the invoices the base seed gave them (the parent app's fee screens use them)."""
    return [cmd.students[("6-B", "Aarav Sharma")].id, cmd.students[("2-A", "Diya Sharma")].id]


def seed(cmd, school):
    rng = random.Random(5000)
    today, tz = cmd.today, cmd.tz
    now = timezone.now()
    keep = set(_family_ids(cmd))
    year = AcademicYear.objects.filter(is_current=True).first()
    aarav_t2 = FeeInvoice.objects.filter(student_id=cmd.students[("6-B", "Aarav Sharma")].id, title="Term 2 tuition").first()
    due_t2 = aarav_t2.due_date if aarav_t2 else cmd._school_day(today, 7)
    q3_due = due_t2 + timedelta(days=1)

    # --- structure ----------------------------------------------------------------------------------------------
    for order, (band, grades, tuition, transport, activity) in enumerate(BANDS):
        for head, amount, dues, optional in (
            ("tuition", tuition, [T1_DUE, due_t2], False),
            ("transport", transport, [Q1_DUE, Q2_DUE, q3_due, Q4_DUE], True),
            ("activity", activity, [ACTIVITY_DUE], False),
        ):
            FeeStructure.objects.create(
                academic_year=year.name if year else "2026–27", band=band, grades=grades, head=head, annual_amount=amount,
                due_dates=[d.isoformat() for d in dues], optional=optional, order=order,
            )

    # --- invoices -----------------------------------------------------------------------------------------------
    students = list(Student.objects.filter(is_active=True).exclude(id__in=keep).select_related("class_group").order_by("class_group__grade", "class_group__section", "roll_no"))
    riders = set(StudentTransport.objects.filter(is_active=True).values_list("student_id", flat=True))
    existing = {(sid, title): inv for sid, title, inv in ((i.student_id, i.title, i) for i in FeeInvoice.objects.exclude(student_id__in=keep))}
    # Rohan Gupta's family asked for a refund (moved off the bus route); leave their fee book as it is.
    untouched = {cmd.students[("7-C", "Rohan Gupta")].id} if ("7-C", "Rohan Gupta") in cmd.students else set()

    new = []
    plan = {}  # student id -> "paid" | "overdue_full" | "overdue_activity" | "overdue_transport" | "plan"
    late_due = {}
    for s in students:
        _band, _g, tuition, transport, activity = band_of(s.class_group.grade)
        weak = s.class_group.grade in WEAK
        pre = not s.class_group.grade.isdigit()
        r = rng.random()
        if s.id in untouched:
            plan[s.id] = "paid"
            continue
        if r < (0.22 if weak else 0.09 if pre else 0.115):
            state = rng.choice(["overdue_full"] * 6 + ["overdue_activity", "overdue_transport" if s.id in riders else "overdue_full"])
        elif r < (0.22 + 0.2 if weak else 0.09 + 0.11 if pre else 0.115 + 0.14):
            state = "plan"
        else:
            state = "paid"
        plan[s.id] = state
        if state == "overdue_full" and rng.random() < 0.28:
            late_due[s.id] = rng.choice(LATE_JOIN_DUES)
        t1_due = late_due.get(s.id, T1_DUE)
        rows = [
            ("Term 1 tuition", "tuition", tuition // 2, t1_due),
            ("Activity fee 2026–27", "activity", activity, late_due.get(s.id, ACTIVITY_DUE)),
            ("Term 2 tuition", "tuition", tuition // 2, due_t2),
        ]
        if s.id in riders:
            q = transport // 4
            rows += [("Transport · Q1", "transport", q, Q1_DUE), ("Transport · Q2", "transport", q, Q2_DUE), ("Transport · Q3", "transport", q, q3_due), ("Transport · Q4", "transport", q, Q4_DUE)]
        for title, cat, amount, due in rows:
            inv = existing.get((s.id, title))
            if inv is not None:
                if title == "Term 1 tuition":
                    inv.amount, inv.due_date, inv.paid_amount = Decimal(amount), due, Decimal(0)
                continue
            new.append(FeeInvoice(school=school, student=s, title=title, category=cat, amount=Decimal(amount), due_date=due, paid_amount=Decimal(0)))
    FeeInvoice.objects.bulk_update([i for (sid, t), i in existing.items() if t == "Term 1 tuition" and sid not in untouched], ["amount", "due_date", "paid_amount"], batch_size=1000)
    FeeInvoice.objects.bulk_create(new, batch_size=1000)

    # --- who paid what, when ------------------------------------------------------------------------------------
    by_student = {}
    for inv in FeeInvoice.objects.filter(student_id__in=[s.id for s in students]).exclude(student_id__in=untouched):
        by_student.setdefault(inv.student_id, {})[inv.title] = inv
    pay = []  # (invoice, amount, paid_at)

    def when(month_weights, lo: date, hi: date):
        """A payment time in [lo, hi) (hi before today), month drawn by weight."""
        months = [m for m in month_weights if date(Y, m, 1) < hi and date(Y, m, 28) >= lo]
        month = rng.choices(months, weights=[month_weights[m] for m in months])[0] if months else hi.month
        start = max(lo, date(Y, month, 1))
        end = min(hi - timedelta(days=1), (date(Y, month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
        day = start + timedelta(days=rng.randint(0, max(0, (end - start).days)))
        return datetime.combine(day, time(rng.randint(8, 19), rng.randint(0, 59)), tzinfo=tz)

    plans = []
    for sid, invs in by_student.items():
        state = plan[sid]
        t1 = invs.get("Term 1 tuition")
        act = invs.get("Activity fee 2026–27")
        q1, q2 = invs.get("Transport · Q1"), invs.get("Transport · Q2")
        if q1:
            pay.append((q1, q1.amount, when({4: 80, 5: 20}, date(Y, 4, 1), today)))
        if q2 and state not in ("overdue_transport", "overdue_full"):
            pay.append((q2, q2.amount, when({6: 60, 7: 40}, date(Y, 6, 1), today)))
        if t1 and state in ("paid", "overdue_activity", "overdue_transport"):
            pay.append((t1, t1.amount, when(MONTH_WEIGHTS, date(Y, 4, 1), today)))
        if act and state in ("paid", "overdue_transport", "plan"):
            pay.append((act, act.amount, when(MONTH_WEIGHTS, date(Y, 4, 1), today)))
        if t1 and state == "plan":
            part = (t1.amount / 4).quantize(Decimal("1"))
            pay.append((t1, part, when({7: 70, 8: 30}, date(Y, 7, 1), today)))
            dues = [date(Y, 7, 15), date(Y, 10, 15), date(Y, 11, 15), date(Y, 12, 15)]
            plans.append(InstalmentPlan(
                school=school, invoice=t1, note="Four instalments agreed with the accounts office",
                instalments=[{"due_on": d.isoformat(), "amount": str(part if i < 3 else t1.amount - part * 3)} for i, d in enumerate(dues)],
            ))
        # A few families pay Term 2 early, in September.
        t2 = invs.get("Term 2 tuition")
        if t2 and state == "paid" and rng.random() < 0.07:
            pay.append((t2, t2.amount, when({9: 1}, date(Y, 9, 1), today)))

    # --- today's counter: ~37 receipts through the day so far ---------------------------------------------------------
    paid_ids = {inv.id for inv, _a, _w in pay}
    t2_candidates = [invs["Term 2 tuition"] for sid, invs in by_student.items() if plan[sid] == "paid" and "Term 2 tuition" in invs and invs["Term 2 tuition"].id not in paid_ids]
    q3_candidates = [invs["Transport · Q3"] for sid, invs in by_student.items() if plan[sid] == "paid" and "Transport · Q3" in invs]
    act_candidates = [invs["Activity fee 2026–27"] for sid, invs in by_student.items() if plan[sid] == "overdue_activity"]
    rng.shuffle(t2_candidates)
    rng.shuffle(q3_candidates)
    rng.shuffle(act_candidates)
    todays = t2_candidates[:5] + q3_candidates[:24] + act_candidates[:4]
    plan_invoices = [p.invoice for p in plans]
    rng.shuffle(plan_invoices)
    today_rows = [(inv, inv.amount) for inv in todays]
    for inv in plan_invoices[:4]:
        today_rows.append((inv, Decimal(next(x for x in next(p for p in plans if p.invoice is inv).instalments[1:])["amount"])))
    rng.shuffle(today_rows)
    # The counter opens at 8:40; receipts are spread from then until now (or 5:30 PM when seeding in the evening).
    opens = datetime.combine(today, time(8, 40), tzinfo=tz)
    lo, hi = (opens, min(now, datetime.combine(today, time(17, 30), tzinfo=tz))) if now > opens + timedelta(minutes=40) else (datetime.combine(today, time(0, 5), tzinfo=tz), now)
    step = (hi - lo) / max(1, len(today_rows))
    for i, (inv, amount) in enumerate(today_rows):
        pay.append((inv, amount, lo + step * i + timedelta(seconds=rng.randint(0, max(1, int(step.total_seconds() * 0.8))))))

    # --- payments, oldest first, with gapless receipt numbers -------------------------------------------------------
    guardians = _families(cmd, school, rng, students, plan)
    pay.sort(key=lambda row: row[2])
    payments = []
    methods, weights = zip(*METHODS)
    for inv, amount, at in pay:
        method = rng.choices(methods, weights=weights)[0]
        if method in ("cash", "cheque") and at.weekday() == 6:
            at -= timedelta(days=1)
        payments.append(Payment(
            school=school, invoice=inv, amount=amount, gateway="counter" if method in ("cash", "cheque") else "mock", status=Payment.Status.SUCCEEDED,
            method=method, gateway_order_id=MARK, gateway_payment_id="" if method in ("cash", "cheque") else f"pay_{uuid.uuid4().hex[:14]}", paid_by=guardians.get(inv.student_id),
            paid_at=at, receipt_no=next_receipt_no(school, at.astimezone(tz).date()),
        ))
        inv.paid_amount = min(inv.amount, inv.paid_amount + amount)
    Payment.objects.bulk_create(payments, batch_size=1000)
    touched = {inv.id: inv for inv, _a, _w in pay}
    FeeInvoice.objects.bulk_update(list(touched.values()), ["paid_amount"], batch_size=1000)
    InstalmentPlan.objects.bulk_create(plans)

    _reminders(cmd, school, rng, today, tz, guardians, plan)
    _back_every_rupee(school, today, tz)


def _families(cmd, school, rng, students, plan):
    """Every student who owes money (overdue or on a plan) gets a guardian; brothers and sisters share one."""
    owing = [s for s in students if plan[s.id] != "paid"]
    links = {}
    for link in StudentGuardian.objects.filter(student_id__in=[s.id for s in students]).select_related("user").order_by("-is_primary", "created_at"):
        links.setdefault(link.student_id, link.user)
    guardians = dict(links)
    n = iter(PHONES)
    by_surname = {}
    first_names = list(FATHERS)
    for s in owing:
        if s.id in guardians:
            continue
        surname = s.full_name.split()[-1]
        # A second overdue child with the same surname in another class joins that family (a sibling).
        sib = next((g for g, kids in by_surname.get(surname, []) if len(kids) == 1 and kids[0].class_group_id != s.class_group_id and plan[kids[0].id] == plan[s.id]), None)
        if sib is not None and rng.random() < 0.55:
            StudentGuardian.objects.create(student=s, user=sib, relationship="father", is_primary=True)
            guardians[s.id] = sib
            for g, kids in by_surname[surname]:
                if g is sib:
                    kids.append(s)
            continue
        mother = rng.random() < 0.3
        first = rng.choice(MOTHERS) if mother else (first_names.pop(0) if first_names else rng.choice(FATHERS))
        user = cmd._user(cmd._phone(next(n)), f"{first} {surname}", school, Role.PARENT, title="Parent")
        StudentGuardian.objects.create(student=s, user=user, relationship="mother" if mother else "father", is_primary=True)
        guardians[s.id] = user
        by_surname.setdefault(surname, []).append((user, [s]))
    return guardians


def _reminders(cmd, school, rng, today, tz, guardians, plan):
    """Two reminder runs (18 and 7 days ago) plus individual WhatsApps and calls; about one family in six never reminded."""
    staff = User.objects.filter(phone=cmd._phone(40)).first() or cmd.principal
    overdue = {}
    for sid, state in plan.items():
        if state.startswith("overdue") and sid in guardians:
            overdue.setdefault(guardians[sid].id, (guardians[sid], []))[1].append(str(sid))
    families = list(overdue.values())
    rng.shuffle(families)
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    day = lambda n: datetime.combine(cmd._school_day(today, -n), time(10, 30), tzinfo=tz)  # noqa: E731
    rows = []
    for i, (guardian, kids) in enumerate(families):
        roll = rng.random()
        if roll < 0.17:
            continue  # never reminded
        amount = FeeInvoice.objects.filter(student_id__in=kids, due_date__lt=today).aggregate(b=Sum("amount"), p=Sum("paid_amount"))
        owed = (amount["b"] or 0) - (amount["p"] or 0)
        base = dict(school=school, guardian=guardian, student_ids=kids, amount=owed, sent_by=staff)
        if roll < 0.75:
            rows.append(FeeReminder(**base, channels=["app", "sms"], status="delivered", batch=run_a, sent_at=day(16)))
        if roll < 0.9:
            status = rng.choice(["delivered", "read", "not_opened", "read"])
            rows.append(FeeReminder(**base, channels=["app", "sms"], status=status, batch=run_b, sent_at=day(6)))
        extra = rng.random()
        if extra < 0.12:
            rows.append(FeeReminder(**base, channels=["whatsapp"], status="read", sent_at=day(3) + timedelta(hours=1)))
        elif extra < 0.22:
            promise = cmd._school_day(today, rng.randint(1, 4))
            note = rng.choice(CALL_NOTES).format(d=f"{promise.day} {promise:%b}")
            rows.append(FeeReminder(**base, channels=["call"], status="no_answer" if note.startswith("No answer") else "answered", note=note, sent_at=day(2) + timedelta(hours=5)))
        elif roll >= 0.9:
            rows.append(FeeReminder(**base, channels=["sms"], status="delivered", sent_at=day(15) - timedelta(hours=2)))
    FeeReminder.objects.bulk_create(rows, batch_size=1000)


def _back_every_rupee(school, today, tz):
    """Any invoice whose paid amount isn't covered by succeeded payments (from any seed) gets a payment for the gap."""
    paid = dict(Payment.objects.filter(status=Payment.Status.SUCCEEDED).values("invoice_id").annotate(s=Sum("amount")).values_list("invoice_id", "s"))
    rows = []
    for inv in FeeInvoice.objects.filter(paid_amount__gt=0):
        gap = inv.paid_amount - (paid.get(inv.id) or 0)
        if gap > 0:
            day = min(inv.due_date - timedelta(days=5), today - timedelta(days=1))
            at = datetime.combine(max(day, date(Y, 4, 1)), time(11, 0), tzinfo=tz)
            rows.append(Payment(
                school=school, invoice=inv, amount=gap, gateway="mock", status=Payment.Status.SUCCEEDED, method="upi",
                gateway_order_id=MARK, gateway_payment_id=f"pay_{uuid.uuid4().hex[:14]}", paid_at=at, receipt_no=next_receipt_no(school, at.date()),
            ))
    Payment.objects.bulk_create(rows)
    _ = financial_year
