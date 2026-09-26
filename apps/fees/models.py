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
        ACTIVITY = "activity", "Activity"
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


class FeeInvoiceItem(SchoolScopedModel):
    """One fee head on an invoice (tuition, lab, library …)."""

    invoice = models.ForeignKey(FeeInvoice, on_delete=models.CASCADE, related_name="items")
    head = models.CharField(max_length=60)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "created_at"]


class Payment(SchoolScopedModel):
    class Status(models.TextChoices):
        CREATED = "created", "Awaiting payment"
        SUCCEEDED = "succeeded", "Paid"
        FAILED = "failed", "Failed"

    invoice = models.ForeignKey(FeeInvoice, on_delete=models.RESTRICT, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    gateway = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED)
    method = models.CharField(max_length=12, blank=True)  # upi | card | netbanking | cash | cheque
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


class Refund(SchoolScopedModel):
    """Money going back to a family. Approved by the principal; accounts then return it through the original method."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    payment = models.ForeignKey(Payment, on_delete=models.RESTRICT, related_name="refunds")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    fee_head = models.CharField(max_length=60, blank=True)
    reason = models.CharField(max_length=400)
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED)


class FeeHead(models.TextChoices):
    TUITION = "tuition", "Tuition"
    TRANSPORT = "transport", "Transport"
    ACTIVITY = "activity", "Activity"


class FeeStructure(SchoolScopedModel):
    """The published fee for one grade band and one fee head, per student per year. Changing it needs the owner's approval."""

    academic_year = models.CharField(max_length=9)
    band = models.CharField(max_length=40, help_text="Label shown in the table, e.g. 'Pre-primary' or '6–8'")
    grades = models.JSONField(default=list, help_text="ClassGroup.grade values the band covers")
    head = models.CharField(max_length=12, choices=FeeHead.choices)
    annual_amount = models.DecimalField(max_digits=10, decimal_places=2)
    due_dates = models.JSONField(default=list, help_text="ISO dates the instalments fall due")
    optional = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "head"]
        constraints = [models.UniqueConstraint(fields=["school", "academic_year", "band", "head"], name="uniq_fee_structure")]


class InstalmentPlan(SchoolScopedModel):
    """A family agreed to pay an invoice in parts. While the plan holds, the balance is not counted as overdue."""

    invoice = models.OneToOneField(FeeInvoice, on_delete=models.CASCADE, related_name="plan")
    instalments = models.JSONField(default=list, help_text='[{"due_on": "2026-10-15", "amount": "14000"}, ...]')
    note = models.CharField(max_length=200, blank=True)
    agreed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    is_active = models.BooleanField(default=True)


class FeeReminder(SchoolScopedModel):
    """One reminder to one family about overdue fees (a message on one or more channels, or a logged call)."""

    class Channel(models.TextChoices):
        APP = "app", "App"
        SMS = "sms", "SMS"
        WHATSAPP = "whatsapp", "WhatsApp"
        CALL = "call", "Call"

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read, no reply"
        NOT_OPENED = "not_opened", "Not opened"
        FAILED = "failed", "Failed"
        ANSWERED = "answered", "Answered"
        NO_ANSWER = "no_answer", "No answer"

    guardian = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    student_ids = models.JSONField(default=list)
    channels = models.JSONField(default=list)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SENT)
    note = models.CharField(max_length=200, blank=True)
    # Per channel: {"sms": {"provider": "log", "status": "logged", "ref": "..."}}
    deliveries = models.JSONField(default=dict, blank=True)
    # Reminders sent together share a batch (a "reminder run").
    batch = models.UUIDField(null=True, blank=True, db_index=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    sent_at = models.DateTimeField()

    class Meta:
        ordering = ["-sent_at"]
        indexes = [models.Index(fields=["guardian", "-sent_at"], name="fee_reminder_guardian_idx")]
