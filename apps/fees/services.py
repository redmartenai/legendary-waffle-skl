from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.academics.access import guardians_of
from apps.core.utils import school_today
from apps.notifications.models import Category
from apps.notifications.services import notify

from .gateways import gateway_for
from .models import FeeInvoice, Payment, ReceiptCounter


def financial_year(day: date) -> str:
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def next_receipt_no(school, day: date) -> str:
    """Must run inside a transaction; the row lock keeps numbers gapless under concurrency."""
    fy = financial_year(day)
    counter, _ = ReceiptCounter.objects.select_for_update().get_or_create(financial_year=fy)
    number = counter.next_number
    counter.next_number = number + 1
    counter.save(update_fields=["next_number", "updated_at"])
    return f"{school.code}/{fy}/{number:06d}"


def start_checkout(invoice: FeeInvoice, user, idempotency_key: str | None) -> tuple[Payment, dict]:
    if invoice.balance <= 0:
        raise ValidationError({"invoice": "This fee is already paid."})
    if idempotency_key:
        existing = Payment.objects.filter(idempotency_key=idempotency_key, invoice=invoice).first()
        if existing and existing.status == Payment.Status.CREATED:
            return existing, {"order_id": existing.gateway_order_id, "amount_paise": int(existing.amount * 100), "currency": "INR"}
    gateway = gateway_for(invoice.school)
    payment = Payment.objects.create(
        invoice=invoice,
        amount=invoice.balance,
        gateway=gateway.name,
        paid_by=user,
        idempotency_key=idempotency_key or None,
    )
    order = gateway.create_order(payment)
    payment.gateway_order_id = order["order_id"]
    payment.save(update_fields=["gateway_order_id", "updated_at"])
    return payment, order


def confirm_payment(payment: Payment, payload: dict) -> Payment:
    if payment.status == Payment.Status.SUCCEEDED:
        return payment  # webhook and app confirmation can both arrive; apply once
    gateway = gateway_for(payment.invoice.school)
    gateway_payment_id = gateway.verify(payment, payload)
    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related("invoice", "invoice__student").get(pk=payment.pk)
        if payment.status == Payment.Status.SUCCEEDED:
            return payment
        if not gateway_payment_id:
            payment.status = Payment.Status.FAILED
            payment.save(update_fields=["status", "updated_at"])
            return payment
        today = school_today(payment.invoice.school)
        payment.status = Payment.Status.SUCCEEDED
        payment.gateway_payment_id = gateway_payment_id
        payment.paid_at = timezone.now()
        payment.receipt_no = next_receipt_no(payment.invoice.school, today)
        payment.save()
        invoice = FeeInvoice.objects.select_for_update().get(pk=payment.invoice_id)
        invoice.paid_amount = min(invoice.amount, invoice.paid_amount + payment.amount)
        invoice.save(update_fields=["paid_amount", "updated_at"])
        student = invoice.student
        notify(
            list(guardians_of([student]).keys()),
            school=invoice.school,
            category=Category.FEES,
            title="Payment received",
            body=f"₹{format_inr(payment.amount)} for {invoice.title} ({student.first_name}). Receipt {payment.receipt_no}.",
            data={"invoice_id": str(invoice.id), "payment_id": str(payment.id), "type": "payment_received"},
            dedupe_key=f"fees:payment:{payment.id}",
        )
    return payment


def format_inr(amount: Decimal) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567."""
    whole = int(Decimal(amount).quantize(Decimal("1")))
    text = str(abs(whole))
    if len(text) > 3:
        head, tail = text[:-3], text[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        text = ",".join(groups + [tail])
    return ("-" if whole < 0 else "") + text
