"""Fees (prototype ``feeTotal`` / ``feePaid`` / ``scholarshipPct`` / ``FeePayment``; screen documentation
"Fees", "Collections", "Defaulters", refunds in the approvals queue; monitoring rule "Fees overdue").

* A **fee plan** is what a grade pays in an academic year, as dated **instalments**.
* A **student fee** assigns a plan to a student, with an optional scholarship percentage (0-100). What the
  student owes is the instalment amounts less the scholarship.
* A **payment** is money received (UPI, card, cash, bank transfer, cheque), recorded by the office. It gets
  a sequential **receipt number** per school and is idempotent on its client key: a retried request
  returns the same payment instead of charging twice. EduFlow does not take online payments: no payment
  gateway is integrated, so every payment is recorded by staff.
* A **refund** is requested against a payment and decided in the approvals queue; only approved refunds
  reduce what has been paid.
* Overdue = (what was due by today, after scholarship) - (payments - approved refunds), never negative.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.academics.models import AcademicYear, Grade
from eduflow.core.ids import uuid7
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class PaymentMode(models.TextChoices):
    UPI = "upi", "UPI"
    CARD = "card", "Card"
    CASH = "cash", "Cash"
    BANK_TRANSFER = "bank_transfer", "Bank transfer"
    CHEQUE = "cheque", "Cheque"


class RefundStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"


class FeePlan(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="+")
    grade = models.ForeignKey(Grade, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "fees_plan"
        constraints = [
            models.UniqueConstraint(fields=["academic_year", "name"], name="fees_plan_name_uniq"),
            models.UniqueConstraint(fields=["id", "school"], name="fees_plan_id_school_uniq"),
        ]


class Instalment(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    plan = models.ForeignKey(FeePlan, on_delete=models.CASCADE, related_name="instalments")
    label = models.CharField(max_length=60)
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "fees_instalment"
        ordering = ["due_date"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="fees_instalment_amount_check"),
            models.UniqueConstraint(fields=["plan", "label"], name="fees_instalment_label_uniq"),
            models.UniqueConstraint(fields=["id", "school"], name="fees_instalment_id_school_uniq"),
        ]


class StudentFee(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="fees")
    plan = models.ForeignKey(FeePlan, on_delete=models.PROTECT, related_name="assignments")
    scholarship_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    scholarship_note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "fees_student_fee"
        constraints = [
            models.UniqueConstraint(fields=["student", "plan"], name="fees_student_plan_uniq"),
            models.CheckConstraint(
                condition=Q(scholarship_percent__gte=0) & Q(scholarship_percent__lte=100),
                name="fees_scholarship_range_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="fees_student_fee_id_school_uniq"),
        ]


class ReceiptCounter(TenantModel):
    """The last receipt number issued by a school (one row per school, locked while issuing)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    last = models.PositiveIntegerField(default=0)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "fees_receipt_counter"
        constraints = [models.UniqueConstraint(fields=["school"], name="fees_receipt_counter_school_uniq")]


class Payment(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="fee_payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    mode = models.CharField(max_length=16, choices=PaymentMode.choices)
    reference = models.CharField(max_length=100, blank=True, help_text="UPI / card / cheque reference.")
    paid_on = models.DateField()
    receipt_number = models.CharField(max_length=32)
    client_key = models.CharField(max_length=64, blank=True)
    note = models.CharField(max_length=300, blank=True)
    collected_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "fees_payment"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="fees_payment_amount_check"),
            models.CheckConstraint(condition=Q(mode__in=PaymentMode.values), name="fees_payment_mode_check"),
            models.UniqueConstraint(fields=["school", "receipt_number"], name="fees_receipt_uniq"),
            models.UniqueConstraint(
                fields=["school", "client_key"], condition=~Q(client_key=""), name="fees_payment_key_uniq"
            ),
            models.UniqueConstraint(fields=["id", "school"], name="fees_payment_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["school", "paid_on"], name="fees_payment_date_idx")]


class Refund(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=16, choices=RefundStatus.choices, default=RefundStatus.PENDING)
    requested_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "fees_refund"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="fees_refund_amount_check"),
            models.UniqueConstraint(fields=["id", "school"], name="fees_refund_id_school_uniq"),
        ]
