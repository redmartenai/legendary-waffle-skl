"""Operations seed: approvals (the principal's web in-tray).

The base seed (``_seed_principal_extras``) files 12 pending requests. This part shapes them toward the design:

- ages a few so three are past the two-day promise, and records the HOD's check on the 8-A Science correction,
  with the corrected answer key and the scanned scripts as evidence;
- adds Farah Khan's leave, Advait Joshi's Grade 6 transfer and Neel Batra's double-charged activity fee;
- settles the base ₹2,500 excursion refund two days ago, so the refunds in the tray are the design's two;
- decides eight requests today (approved, sent back, rejected) for "Decided today".

Everything it makes is removed by ``clear`` (refund → request → payment → invoice); base rows it touches are only
re-aged or re-decided, which is re-applied the same way on every run.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.utils import timezone

ORDER_ID = "seed-approvals"
FARAH_REASON = "Family function in Jaipur; the Art lessons can be swapped with Music."
ARJUN_REASON = "Sister's graduation in Pune."
REKHA_REASON = "Afternoon at the passport office."
KAVYA_MARKS_REASON = "Q3 on page 2 was not marked; the script was scanned without that page."
ATTENDANCE_REASON = "Arrived with the school bus, which was late; asking to mark present."
ADVAIT_NO = "APP-27-0433"
NEEL = ("10-A", 17)
FILES = ("Q7 answer key, corrected", "8-A scripts, Q7 only")
CHECK_NOTE = "Verified against the key. Recommend approval."


def _models():
    from apps.admissions.models import Application
    from apps.approvals.models import ApprovalAttachment, ApprovalEvent, ApprovalRequest
    from apps.attendance.models import AttendanceCorrection
    from apps.fees.models import FeeInvoice, Payment, Refund
    from apps.results.models import MarkCorrection
    from apps.staff.models import StaffLeave

    return Application, ApprovalAttachment, ApprovalEvent, ApprovalRequest, AttendanceCorrection, FeeInvoice, Payment, Refund, MarkCorrection, StaffLeave


def _requests_for(targets):
    from apps.approvals.models import ApprovalRequest

    out = ApprovalRequest.objects.none()
    for t in targets:
        out = out | ApprovalRequest.objects.filter(target_type=ContentType.objects.get_for_model(t), target_id=t.pk)
    return out


def _excursion_refund():
    from apps.fees.models import Refund

    return Refund.objects.filter(fee_head="Excursion", amount=2500, payment__gateway_order_id="seed").first()


def _today_leaves(cmd):
    """The base seed's leave for today (Vikram, Sunita), approved at 7:40 without going through the tray."""
    from apps.staff.models import StaffLeave

    names = [cmd.staff[n] for n in ("Vikram Singh", "Sunita Verma") if n in cmd.staff]
    return list(StaffLeave.objects.filter(user__in=names, from_date=cmd.today, status="approved"))


def clear(cmd, school):
    Application, ApprovalAttachment, ApprovalEvent, ApprovalRequest, AttendanceCorrection, FeeInvoice, Payment, Refund, MarkCorrection, StaffLeave = _models()
    from apps.accounts.models import User

    for doc in ApprovalAttachment.objects.filter(name__in=FILES):
        doc.file.delete(save=False)
        doc.delete()

    payments = list(Payment.objects.filter(gateway_order_id=ORDER_ID))
    refunds = list(Refund.objects.filter(payment__in=payments))
    leaves = list(StaffLeave.objects.filter(reason__in=[FARAH_REASON, ARJUN_REASON, REKHA_REASON]))
    marks = list(MarkCorrection.objects.filter(reason=KAVYA_MARKS_REASON))
    fixes = list(AttendanceCorrection.objects.filter(reason=ATTENDANCE_REASON))
    apps_ = list(Application.objects.filter(application_no=ADVAIT_NO))
    _requests_for([*refunds, *leaves, *marks, *fixes, *apps_, *_today_leaves(cmd)]).delete()
    Refund.objects.filter(pk__in=[r.pk for r in refunds]).delete()
    invoices = {p.invoice_id for p in payments}
    Payment.objects.filter(pk__in=[p.pk for p in payments]).delete()
    FeeInvoice.objects.filter(pk__in=invoices).delete()
    for obj in (*leaves, *marks, *fixes, *apps_):
        obj.delete()

    # The base excursion refund goes back to waiting.
    excursion = _excursion_refund()
    if excursion:
        for req in _requests_for([excursion]):
            req.events.exclude(action=ApprovalEvent.Action.SUBMITTED).delete()
            ApprovalRequest.objects.filter(pk=req.pk).update(status="pending", decided_by=None, decided_at=None, decision_note="")
        Refund.objects.filter(pk=excursion.pk).update(status="requested")

    User.objects.filter(phone__in=[cmd._phone(n) for n in range(5800, 5810)]).delete()


def seed(cmd, school):
    Application, ApprovalAttachment, ApprovalEvent, ApprovalRequest, AttendanceCorrection, FeeInvoice, Payment, Refund, MarkCorrection, StaffLeave = _models()
    from apps.academics.models import Student, StudentGuardian
    from apps.accounts.models import Role, User
    from apps.approvals.services import open_request, record_check
    from apps.attendance.models import AttendanceException
    from apps.core.management.commands.seed_design import _pdf
    from apps.fees.services import next_receipt_no
    from apps.results.models import Exam, ExamMark

    now = timezone.now()
    today, tz = cmd.today, cmd.tz
    staff, principal = cmd.staff, cmd.principal
    accounts = User.objects.filter(phone=cmd._phone(40)).first()
    office = User.objects.filter(phone=cmd._phone(43)).first() or accounts
    day_start = datetime.combine(today, time(0, 1), tzinfo=tz)

    def aged(req, when):
        ApprovalRequest.objects.filter(pk=req.pk).update(created_at=when)
        ApprovalEvent.objects.filter(request=req, action=ApprovalEvent.Action.SUBMITTED).update(created_at=when)

    def base(kind, contains):
        return ApprovalRequest.objects.filter(kind=kind, summary__contains=contains).order_by("created_at").first()

    def today_at(h, m, slot):
        """A time today for a decision, never later than now (earlier slots stay earlier)."""
        return max(day_start, min(datetime.combine(today, time(h, m), tzinfo=tz), now - timedelta(minutes=20 * (9 - slot))))

    def decided(req, status, note, when, actor=None):
        actor = actor or principal
        ApprovalRequest.objects.filter(pk=req.pk).update(status=status, decided_by=actor, decided_at=when, decision_note=note)
        event = ApprovalEvent.objects.create(request=req, actor=actor, action=status, note=note, device="EduFlow console · Chrome on macOS")
        ApprovalEvent.objects.filter(pk=event.pk).update(created_at=when)

    def pay(student, title, category, amount, paid_at, method, payer):
        inv = FeeInvoice.objects.create(student=student, title=title, category=category, amount=Decimal(amount), due_date=paid_at.date(), paid_amount=Decimal(amount))
        p = Payment.objects.create(
            invoice=inv, amount=Decimal(amount), gateway="mock", status=Payment.Status.SUCCEEDED, method=method, gateway_order_id=ORDER_ID,
            gateway_payment_id=ORDER_ID, paid_by=payer, paid_at=paid_at, receipt_no=next_receipt_no(school, paid_at.date()),
        )
        return inv, p

    def parent_of(student, n, name, relationship):
        user = cmd._user(cmd._phone(n), name, school, Role.PARENT, title="Parent")
        StudentGuardian.objects.get_or_create(student=student, user=user, defaults={"relationship": relationship, "is_primary": False})
        return user

    # --- Base requests: ages that match the design (three past the two-day promise) ---
    for kind, contains, hours in (("refund", "Rohan Gupta", 73), ("admission", "APP-27-0412", 25), ("admission", "APP-27-0398", 30)):
        req = base(kind, contains)
        if req and req.status == "pending":
            aged(req, now - timedelta(hours=hours))
    for name, hours in (("Kavya Nair", 5), ("Ravi Kumar", 74), ("Deepa Iyer", 2), ("Priya Menon", 26)):
        for req in ApprovalRequest.objects.filter(kind="leave", status="pending", requested_by=staff.get(name)):
            aged(req, now - timedelta(hours=hours))

    # --- The 8-A Science correction: the HOD's check and the evidence ---
    sci = base("marks", "8-A Science")
    if sci:
        checked_at = min(sci.created_at + timedelta(minutes=57), now - timedelta(minutes=5))
        check = sci.events.filter(action=ApprovalEvent.Action.CHECKED).first()
        if check:
            ApprovalEvent.objects.filter(pk=check.pk).update(created_at=checked_at, note=CHECK_NOTE)
        elif sci.target.checked_by:
            record_check(sci, sci.target.checked_by, note=CHECK_NOTE, when=checked_at)
        key = _pdf("Unit Test 2 · Science · 8-A · Answer key (corrected)", "Q7: the correct option is (c). The earlier key said (b). 4 marks.")
        ApprovalAttachment.objects.create(request=sci, file=ContentFile(key, name="q7-answer-key-corrected.pdf"), name=FILES[0], kind="document", pages=1, size=len(key), uploaded_by=sci.requested_by)
        scripts = _scripts(sci.target)
        ApprovalAttachment.objects.create(request=sci, file=ContentFile(scripts, name="8a-scripts-q7.pdf"), name=FILES[1], kind="scan", pages=3, size=len(scripts), uploaded_by=sci.requested_by)

    # --- New pending requests from the design ---
    farah = staff.get("Farah Khan")
    if farah:
        start, end = cmd._school_day(today, 4), cmd._school_day(today, 5)
        leave = StaffLeave.objects.create(user=farah, kind="casual", from_date=start, to_date=end, days=2, reason=FARAH_REASON)
        aged(open_request(kind="leave", target=leave, requested_by=farah, summary=f"Casual · {start:%d}–{end:%d %b} · 2 days", due_on=start, notify_principal=False), now - timedelta(hours=8))

    advait = Application.objects.create(
        application_no=ADVAIT_NO, child_name="Advait Joshi", grade="6", academic_year="2027–28", guardian_name="Sanjay Joshi", documents_verified=False,
        documents_pending="Transfer certificate", interaction_on=cmd._school_days(today, 5)[0], stage="documents", stage_changed_at=now - timedelta(days=4),
        source="referral", enquired_on=today - timedelta(days=24), form_fee_paid=True, assessment_score=78, assessment_out_of=100,
    )
    aged(open_request(kind="admission", target=advait, requested_by=office, summary=f"Grade 6 transfer · 2027–28 · {ADVAIT_NO}", notify_principal=False), now - timedelta(days=4, hours=2))

    neel = Student.objects.filter(class_group=cmd.groups[NEEL[0]], roll_no=NEEL[1]).first()
    if neel:
        Student.objects.filter(pk=neel.pk).update(full_name="Neel Batra")
        neel.refresh_from_db()
        mother = parent_of(neel, 5800, "Ritu Batra", "mother")
        charged = datetime.combine(today - timedelta(days=3), time(19, 12), tzinfo=tz)
        inv, first = pay(neel, "Activity fee · Term 1", "activity", 12000, charged, "upi", mother)
        # The gateway retried: a second, identical payment two minutes later on the same bill.
        second = Payment.objects.create(
            invoice=inv, amount=Decimal(12000), gateway="mock", status=Payment.Status.SUCCEEDED, method="upi", gateway_order_id=ORDER_ID,
            gateway_payment_id=f"{ORDER_ID}-retry", paid_by=mother, paid_at=charged + timedelta(minutes=2), receipt_no=next_receipt_no(school, charged.date()),
        )
        refund = Refund.objects.create(payment=second, amount=12000, fee_head="Activity fee", reason="Charged twice for the same bill; the UPI app retried the payment.", asked_by=mother)
        aged(open_request(kind="refund", target=refund, requested_by=accounts, summary=f"Neel Batra · {NEEL[0]} · ₹12,000", notify_principal=False), now - timedelta(hours=44))

    # --- The base excursion refund: settled two days ago ---
    excursion = _excursion_refund()
    if excursion:
        req = _requests_for([excursion]).filter(status="pending").first()
        if req:
            decided(req, "approved", "Venue cancelled; refund in full.", now - timedelta(days=2, hours=3))
            Refund.objects.filter(pk=excursion.pk).update(status="approved")

    # --- Decided today: eight requests ---
    slot = iter(range(9))

    def settle(req, status, note, h, m, waited):
        """Decide ``req`` today at about h:m, having been submitted ``waited`` earlier."""
        when = today_at(h, m, next(slot))
        aged(req, when - waited)
        decided(req, status, note, when)

    for leave in sorted(_today_leaves(cmd), key=lambda lv: lv.user.full_name):
        req = open_request(kind="leave", target=leave, requested_by=leave.user, summary=f"{leave.get_kind_display()} · {today:%a %d %b} · 1 day", due_on=today, notify_principal=False)
        sick = leave.kind == "sick"
        settle(req, "approved", "Cover arranged", 7, 44 if sick else 48, timedelta(minutes=54) if sick else timedelta(days=2, hours=4))

    arjun = staff.get("Arjun Das")
    if arjun:
        day = cmd._school_day(today, 12)
        leave = StaffLeave.objects.create(user=arjun, kind="casual", from_date=day, to_date=day, days=1, reason=ARJUN_REASON, status="approved", decided_by=principal, decided_at=today_at(9, 10, 2))
        req = open_request(kind="leave", target=leave, requested_by=arjun, summary=f"Casual · {day:%a %d %b} · 1 day", due_on=day, notify_principal=False)
        settle(req, "approved", "", 9, 10, timedelta(days=1, hours=3))

    ut2 = Exam.objects.filter(class_group__grade="7", class_group__section="A", name="Unit Test 2", is_published=True).first()
    kavya = staff.get("Kavya Nair")
    if ut2 and kavya:
        mark = ExamMark.objects.filter(exam=ut2, subject__code="ENG", is_absent=False).select_related("subject").order_by("student__roll_no").first()
        if mark:
            corr = MarkCorrection.objects.create(
                exam=ut2, subject=mark.subject, reason=KAVYA_MARKS_REASON,
                entries=[{"student_id": str(mark.student_id), "from": float(mark.marks), "to": float(min(mark.max_marks, mark.marks + 3)), "note": "Q3 · not marked"}],
            )
            req = open_request(kind="marks", target=corr, requested_by=kavya, summary="UT2 · 7-A English · 1 change", notify_principal=False)
            settle(req, "sent_back", "Scanned script missing; attach page 2.", 10, 5, timedelta(hours=22))

    rekha = staff.get("Rekha Kulkarni")
    if rekha:
        day = cmd._school_day(today, 3)
        leave = StaffLeave.objects.create(user=rekha, kind="casual", from_date=day, to_date=day, half_day=True, days=Decimal("0.5"), reason=REKHA_REASON, status="approved", decided_by=principal, decided_at=today_at(10, 40, 4))
        req = open_request(kind="leave", target=leave, requested_by=rekha, summary=f"Casual · {day:%a %d %b} · half day", due_on=day, notify_principal=False)
        settle(req, "approved", "Sameer Naik covers 6-A registration.", 10, 40, timedelta(hours=16))

    books = Student.objects.filter(class_group=cmd.groups.get("3-C")).order_by("roll_no").first()
    if books:
        payer = parent_of(books, 5801, f"Kiran {books.full_name.split()[-1]}", "father")
        _, p = pay(books, "Books · 2026–27", "other", 2400, datetime.combine(today - timedelta(days=40), time(18, 30), tzinfo=tz), "upi", payer)
        refund = Refund.objects.create(payment=p, amount=2400, fee_head="Book fee", reason="The set was bought twice; the sibling's books are being reused.", asked_by=payer, status="approved")
        req = open_request(kind="refund", target=refund, requested_by=accounts, summary=f"{books.full_name} · 3-C · ₹2,400", notify_principal=False)
        settle(req, "approved", "Paid back by UPI", 11, 20, timedelta(days=1, hours=6))

    uniform = Student.objects.filter(class_group=cmd.groups.get("4-B")).order_by("-roll_no").first()
    if uniform:
        payer = parent_of(uniform, 5802, f"Anjali {uniform.full_name.split()[-1]}", "mother")
        _, p = pay(uniform, "Uniform · winter set", "other", 1800, datetime.combine(today - timedelta(days=12), time(17, 5), tzinfo=tz), "card", payer)
        refund = Refund.objects.create(payment=p, amount=1800, fee_head="Uniform", reason="Wrong size delivered.", asked_by=payer)
        req = open_request(kind="refund", target=refund, requested_by=accounts, summary=f"{uniform.full_name} · 4-B · ₹1,800", notify_principal=False)
        settle(req, "sent_back", "Exchange the size at the store first; refund only if they can't.", 11, 55, timedelta(hours=20))
        Refund.objects.filter(pk=refund.pk).update(status="declined")

    late = AttendanceException.objects.filter(status="late", session__date__lte=today).select_related("session__class_group").order_by("-session__date").first()
    if late:
        fix = AttendanceCorrection.objects.create(
            session=late.session, entries=[{"student_id": str(late.student_id), "from": "late", "to": "present"}], reason=ATTENDANCE_REASON,
            requested_by=late.session.marked_by or staff.get("Deepa Iyer"),
        )
        req = open_request(kind="attendance", target=fix, requested_by=fix.requested_by, summary=f"{late.session.class_group.short_label} register · {late.session.date:%a %d %b} · 1 change", notify_principal=False)
        settle(req, "declined", "The gate log shows 8:21; the bus was on time. Late stays.", 12, 40, timedelta(hours=3, minutes=10))

    cmd.stdout.write(
        f"Approvals: {ApprovalRequest.objects.filter(status='pending').count()} pending, "
        f"{ApprovalRequest.objects.filter(decided_at__gte=day_start).exclude(status='pending').count()} decided today."
    )


def _scripts(corr) -> bytes:
    """Three scanned answer-script pages (Q7 only), one per student in the correction."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from apps.academics.models import Student

    names = dict(Student.objects.filter(id__in=[e["student_id"] for e in corr.entries]).values_list("id", "full_name"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("8-A scripts, Q7 only")
    for e in corr.entries:
        name = next((v for k, v in names.items() if str(k) == e["student_id"]), "")
        c.setFont("Helvetica-Bold", 16)
        c.drawString(60, 780, "Sunrise Public School · Unit Test 2 · Science")
        c.setFont("Helvetica", 12)
        c.drawString(60, 755, f"{name} · 8-A")
        c.rect(60, 560, 470, 170)
        c.drawString(75, 705, "Q7. Which of these is a chemical change?")
        c.drawString(75, 680, "Answer: (c) Rusting of iron")
        c.setFont("Helvetica-Oblique", 11)
        c.drawString(75, 600, f"Marked 0/4 against the old key; {e['to'] - e['from']:+g} after correction.")
        c.showPage()
    c.save()
    return buf.getvalue()
