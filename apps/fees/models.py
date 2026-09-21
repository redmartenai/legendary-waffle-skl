from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import SchoolScopedModel


class FeeInvoice(SchoolScopedModel):
    class Category(models.TextChoices):
        TUITION = "tuition", "Tuition"
        TRANSPORT = "transport", "Transport"
        HOSTEL = "hostel", "Hostel"
        EXAM = "exam", "Exam"
        OTHER = "other", "Other"

    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="invoices")
    title = models.CharField(max_length=120)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.TUITION)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    due_date = models.DateField()
    # Tuition, transport, hostel and exam fees from a recognised school/college are GST-exempt.
    gst_exempt = models.BooleanField(default=True)

    class Meta:
        ordering = ["due_date"]

    @property
    def balance(self) -> Decimal:
        return max(Decimal("0"), self.amount - self.paid_amount)

    def status_on(self, today) -> str:
        if self.balance == 0:
            return "paid"
        if self.due_date < today:
            return "overdue"
        return "partial" if self.paid_amount > 0 else "due"


class Payment(SchoolScopedModel):
    class Status(models.TextChoices):
        CREATED = "created", "Awaiting payment"
        SUCCEEDED = "succeeded", "Paid"
        FAILED = "failed", "Failed"

    invoice = models.ForeignKey(FeeInvoice, on_delete=models.RESTRICT, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    gateway = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED)
    gateway_order_id = models.CharField(max_length=80, blank=True)
    gateway_payment_id = models.CharField(max_length=80, blank=True)
    receipt_no = models.CharField(max_length=40, null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "receipt_no"], condition=Q(receipt_no__isnull=False), name="uniq_receipt_no"
            ),
            models.UniqueConstraint(
                fields=["school", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="uniq_payment_idempotency",
            ),
        ]


class ReceiptCounter(SchoolScopedModel):
    """Gapless receipt numbers per school and financial year (April–March)."""

    financial_year = models.CharField(max_length=9)
    next_number = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "financial_year"], name="uniq_receipt_counter")]
