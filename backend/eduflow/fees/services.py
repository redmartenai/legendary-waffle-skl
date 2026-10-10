"""Fee writes and balances. Transactional and audited (``fees.*``). Money is ``Decimal`` throughout, rounded
to paise with ROUND_HALF_UP."""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from eduflow.academics.models import AcademicYear, Grade, Section
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people.models import Student
from eduflow.tenancy import clock, domain

from .models import (
    FeePlan,
    Instalment,
    Payment,
    ReceiptCounter,
    Refund,
    RefundStatus,
    StudentFee,
)

CENT = Decimal("0.01")
ZERO = Decimal(0)


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


# ------------------------------------------------------------------------------------------------ plans
def _instalments(plan: FeePlan, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    labels = [r["label"] for r in rows]
    if len(set(labels)) != len(labels):
        raise ValidationError({"instalments": ["Instalment labels must be unique."]})
    year = plan.academic_year
    for i, r in enumerate(rows):
        if not year.start_date <= r["due_date"] <= year.end_date:
            raise ValidationError(
                {f"instalments[{i}].due_date": ["The due date is outside the academic year."]}
            )
    plan.instalments.all().delete()
    Instalment.objects.bulk_create([Instalment(school_id=plan.school_id, plan=plan, **r) for r in rows])


@transaction.atomic
def create_plan(
    actor: Actor, *, name: str, academic_year_id: Any, instalments: list[dict[str, Any]], grade_id: Any = None
) -> FeePlan:
    year = domain.resolve(AcademicYear, actor.school, academic_year_id, "academic_year_id")
    grade = domain.resolve(Grade, actor.school, grade_id, "grade_id") if grade_id else None
    plan = FeePlan(school=actor.school, name=name, academic_year=year, grade=grade)
    domain.save(plan, conflict="A fee plan with this name already exists in the academic year.")
    _instalments(plan, instalments)
    domain.record("fees.plan.created", plan, total=str(plan_total(plan)))
    return plan


@transaction.atomic
def update_plan(actor: Actor, plan: FeePlan, **data: Any) -> FeePlan:
    rows = data.pop("instalments", None)
    changed = domain.apply_changes(plan, data)
    if changed:
        domain.save(plan, conflict="A fee plan with this name already exists in the academic year.")
    if rows is not None:
        _instalments(plan, rows)
        changed.append("instalments")
    if changed:
        domain.record("fees.plan.updated", plan, fields=changed, total=str(plan_total(plan)))
    return plan


def plan_total(plan: FeePlan) -> Decimal:
    return plan.instalments.aggregate(total=Sum("amount"))["total"] or ZERO


# ------------------------------------------------------------------------------------------------ obligations
def _check_scholarship(percent: Decimal) -> None:
    if not ZERO <= percent <= 100:
        raise ValidationError({"scholarship_percent": ["Between 0 and 100."]})


@transaction.atomic
def assign(
    actor: Actor,
    *,
    student_id: Any,
    plan_id: Any,
    scholarship_percent: Decimal = ZERO,
    scholarship_note: str = "",
) -> StudentFee:
    student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
    plan = domain.resolve(FeePlan, actor.school, plan_id, "plan_id", label="fee plan")
    _check_scholarship(scholarship_percent)
    fee = StudentFee(
        school=actor.school,
        student=student,
        plan=plan,
        scholarship_percent=scholarship_percent,
        scholarship_note=scholarship_note,
    )
    domain.save(fee, conflict="This student already has this fee plan.")
    domain.record("fees.student_fee.assigned", fee, student=str(student.pk), plan=str(plan.pk))
    return fee


@transaction.atomic
def assign_section(actor: Actor, plan: FeePlan, *, section_id: Any) -> int:
    """Assign the plan to every actively enrolled student of a section who does not have it yet."""
    section = domain.resolve(Section, actor.school, section_id, "section_id", label="section")
    if section.academic_year_id != plan.academic_year_id:
        raise ValidationError({"section_id": ["This section is in another academic year."]})
    if plan.grade_id is not None and section.grade_id != plan.grade_id:
        raise ValidationError({"section_id": ["This plan is for another grade."]})
    students = Student.objects.filter(enrollments__section=section, enrollments__status="active").exclude(
        fees__plan=plan
    )
    created = StudentFee.objects.bulk_create(
        [StudentFee(school=actor.school, student=s, plan=plan) for s in students.distinct()]
    )
    domain.record("fees.plan.assigned_to_section", plan, section=str(section.pk), students=len(created))
    return len(created)


@transaction.atomic
def update_obligation(actor: Actor, fee: StudentFee, **data: Any) -> StudentFee:
    if "scholarship_percent" in data:
        _check_scholarship(data["scholarship_percent"])
    changed = domain.apply_changes(fee, data)
    if changed:
        fee.save()
        domain.record(
            "fees.student_fee.updated", fee, fields=changed, scholarship=str(fee.scholarship_percent)
        )
    return fee


# ------------------------------------------------------------------------------------------------ payments
def _next_receipt(school: Any, on: datetime.date) -> str:
    counter, _ = ReceiptCounter.objects.select_for_update().get_or_create(school_id=school.pk)
    counter.last += 1
    counter.save(update_fields=["last"])
    return f"R-{on.year}-{counter.last:06d}"


@transaction.atomic
def record_payment(
    actor: Actor,
    *,
    student_id: Any,
    amount: Decimal,
    mode: str,
    paid_on: datetime.date | None = None,
    reference: str = "",
    note: str = "",
    client_key: str = "",
) -> tuple[Payment, bool]:
    """Record money received. Returns ``(payment, replayed)``: the same ``client_key`` returns the first
    payment unchanged (a retried request never records the money twice)."""
    if client_key:
        existing = Payment.objects.filter(school_id=actor.school.pk, client_key=client_key).first()
        if existing is not None:
            if existing.student_id != student_id or existing.amount != amount:
                raise Conflict("This idempotency key was used for a different payment.")
            return existing, True
    student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
    today = clock.today(actor.school)
    paid_on = paid_on or today
    if paid_on > today:
        raise ValidationError({"paid_on": ["A payment cannot be dated in the future."]})
    payment = Payment(
        school=actor.school,
        student=student,
        amount=money(amount),
        mode=mode,
        reference=reference,
        paid_on=paid_on,
        note=note,
        client_key=client_key,
        collected_by=actor.membership,
        receipt_number=_next_receipt(actor.school, paid_on),
    )
    domain.save(payment, conflict="This payment was already recorded.")
    domain.record(
        "fees.payment.recorded",
        payment,
        amount=str(payment.amount),
        mode=mode,
        receipt=payment.receipt_number,
    )
    notifications.notify(
        actor.school,
        notifications.family_of(student),
        kind="fees",
        title=f"Payment received: {payment.amount}",
        body=f"Receipt {payment.receipt_number}",
        student=student,
        link=("fee_payment", payment.pk),
    )
    return payment, False


def refundable(payment: Payment) -> Decimal:
    taken = payment.refunds.exclude(status=RefundStatus.DECLINED).aggregate(t=Sum("amount"))["t"] or ZERO
    return payment.amount - taken


@transaction.atomic
def request_refund(actor: Actor, payment: Payment, *, amount: Decimal, reason: str) -> Refund:
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if money(amount) > refundable(payment):
        raise ValidationError({"amount": [f"At most {refundable(payment)} can still be refunded."]})
    refund = Refund.objects.create(
        school=actor.school,
        payment=payment,
        amount=money(amount),
        reason=reason,
        requested_by=actor.membership,
    )
    domain.record("fees.refund.requested", refund, payment=str(payment.pk), amount=str(refund.amount))
    return refund


@transaction.atomic
def decide_refund(actor: Actor, refund: Refund, decision: str, note: str = "") -> Refund:
    refund = Refund.objects.select_for_update(of=("self",)).get(pk=refund.pk)
    if refund.status != RefundStatus.PENDING:
        raise Conflict("This refund has already been decided.")
    refund.status = RefundStatus.APPROVED if decision == "approve" else RefundStatus.DECLINED
    refund.decided_by, refund.decided_at, refund.decision_note = actor.membership, timezone.now(), note
    refund.save()
    domain.record("fees.refund.decided", refund, decision=refund.status, amount=str(refund.amount))
    return refund


# ------------------------------------------------------------------------------------------------ balances
@dataclass
class Statement:
    student: Student
    total: Decimal = ZERO
    due_to_date: Decimal = ZERO
    paid: Decimal = ZERO
    refunded: Decimal = ZERO
    lines: list[dict[str, Any]] = field(default_factory=list)

    @property
    def net_paid(self) -> Decimal:
        return self.paid - self.refunded

    @property
    def balance(self) -> Decimal:
        return self.total - self.net_paid

    @property
    def overdue(self) -> Decimal:
        return max(ZERO, self.due_to_date - self.net_paid)

    @property
    def next_due(self) -> datetime.date | None:
        """The first instalment not yet covered by what was paid (instalments are paid oldest first)."""
        covered = self.net_paid
        for line in self.lines:
            covered -= line["amount"]
            if covered < 0:
                return line["due_date"]  # type: ignore[no-any-return]
        return None


def statement(student: Student, today: datetime.date) -> Statement:
    st = Statement(student=student)
    fees = (
        StudentFee.objects.filter(student=student)
        .select_related("plan")
        .prefetch_related("plan__instalments")
    )
    for fee in fees:
        factor = (Decimal(100) - fee.scholarship_percent) / Decimal(100)
        for inst in fee.plan.instalments.all():
            amount = money(inst.amount * factor)
            st.total += amount
            if inst.due_date <= today:
                st.due_to_date += amount
            st.lines.append(
                {"plan": fee.plan.name, "label": inst.label, "due_date": inst.due_date, "amount": amount}
            )
    st.lines.sort(key=lambda line: line["due_date"])
    st.paid = Payment.objects.filter(student=student).aggregate(t=Sum("amount"))["t"] or ZERO
    st.refunded = (
        Refund.objects.filter(payment__student=student, status=RefundStatus.APPROVED).aggregate(
            t=Sum("amount")
        )["t"]
        or ZERO
    )
    return st


def defaulters(students: Iterable[Student], today: datetime.date) -> list[Statement]:
    """Statements with something overdue, the largest first (the "Fees overdue" rule reads these)."""
    rows = [statement(s, today) for s in students]
    return sorted((r for r in rows if r.overdue > 0), key=lambda r: r.overdue, reverse=True)


def collections(school: Any, since: datetime.date, until: datetime.date) -> dict[str, Any]:
    """Money received in a period, by mode, net of refunds approved in it (accountant dashboard)."""
    payments = Payment.objects.filter(school_id=school.pk, paid_on__gte=since, paid_on__lte=until)
    by_mode = {row["mode"]: row["t"] for row in payments.values("mode").annotate(t=Sum("amount"))}
    refunded = (
        Refund.objects.filter(
            school_id=school.pk,
            status=RefundStatus.APPROVED,
            decided_at__date__gte=since,
            decided_at__date__lte=until,
        ).aggregate(t=Sum("amount"))["t"]
        or ZERO
    )
    received = sum(by_mode.values(), ZERO)
    return {"received": received, "refunded": refunded, "net": received - refunded, "by_mode": by_mode}
