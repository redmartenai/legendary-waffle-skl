import hashlib
import hmac
import json
from decimal import Decimal

from django.http import Http404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.academics.access import student_for_request
from apps.accounts.models import Role
from apps.core.api import SchoolAPIView
from apps.core.tenant import unscoped, use_school
from apps.core.utils import school_today

from . import services
from .models import FeeInvoice, Payment


def money(value: Decimal) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def invoice_payload(invoice: FeeInvoice, today) -> dict:
    return {
        "id": str(invoice.id),
        "title": invoice.title,
        "category": invoice.category,
        "amount": str(invoice.amount),
        "paid_amount": str(invoice.paid_amount),
        "balance": str(invoice.balance),
        "due_date": invoice.due_date.isoformat(),
        "status": invoice.status_on(today),
        "gst_exempt": invoice.gst_exempt,
        "items": [{"head": i.head, "amount": str(i.amount)} for i in invoice.items.all()],
    }


def payment_payload(payment: Payment) -> dict:
    return {
        "id": str(payment.id),
        "invoice_id": str(payment.invoice_id),
        "invoice_title": payment.invoice.title,
        "amount": str(payment.amount),
        "status": payment.status,
        "receipt_no": payment.receipt_no,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        "gateway": payment.gateway,
        "method": payment.method or None,
        "download": f"/fees/payments/{payment.id}/receipt.pdf" if payment.receipt_no else None,
    }


class StudentFeesView(SchoolAPIView):
    def get(self, request, student_id):
        student = student_for_request(request, student_id, purpose="fees")
        today = school_today(request.school)
        invoices = list(FeeInvoice.objects.filter(student=student).prefetch_related("items"))
        payments = Payment.objects.filter(invoice__student=student, status=Payment.Status.SUCCEEDED).select_related("invoice")[:20]
        outstanding = [i for i in invoices if i.balance > 0]
        next_due = min(outstanding, key=lambda i: i.due_date) if outstanding else None
        total = sum((i.amount for i in invoices), start=Decimal("0"))
        paid = sum((i.paid_amount for i in invoices), start=Decimal("0"))
        # "Due soon" is what falls due in the next 45 days; the rest is later in the year.
        soon = [i for i in outstanding if (i.due_date - today).days <= 45]
        return Response(
            {
                "year": {
                    "total": money(total),
                    "paid": money(paid),
                    "due_soon": money(sum((i.balance for i in soon), start=Decimal("0"))),
                    "due_soon_by": max((i.due_date for i in soon), default=None),
                    "later": money(sum((i.balance for i in outstanding if i not in soon), start=Decimal("0"))),
                },
                "total_due": money(sum((i.balance for i in outstanding), start=Decimal("0"))),
                "overdue": any(i.status_on(today) == "overdue" for i in outstanding),
                "next_due_date": next_due.due_date.isoformat() if next_due else None,
                "invoices": [invoice_payload(i, today) for i in invoices],
                "receipts": [payment_payload(p) for p in payments],
            }
        )


class InvoiceCheckoutView(SchoolAPIView):
    allowed_roles = frozenset({Role.PARENT, Role.STUDENT, Role.ACCOUNTANT})

    def post(self, request, invoice_id):
        invoice = FeeInvoice.objects.filter(id=invoice_id).select_related("student").first()
        if invoice is None:
            raise Http404
        student_for_request(request, invoice.student_id, purpose="fees")
        key = request.META.get("HTTP_IDEMPOTENCY_KEY") or request.data.get("idempotency_key")
        payment, order = services.start_checkout(invoice, request.user, key)
        method = str(request.data.get("method") or "")
        if method in {"upi", "card", "netbanking"} and payment.method != method:
            payment.method = method
            payment.save(update_fields=["method", "updated_at"])
        return Response(
            {"payment_id": str(payment.id), "gateway": payment.gateway, "amount": str(payment.amount), "order": order},
            status=status.HTTP_201_CREATED,
        )


class PaymentConfirmView(SchoolAPIView):
    """Called by the app after checkout. The server verifies with the gateway before marking paid."""

    def post(self, request, payment_id):
        payment = Payment.objects.filter(id=payment_id).select_related("invoice", "invoice__student").first()
        if payment is None:
            raise Http404
        student_for_request(request, payment.invoice.student_id, purpose="fees")
        payment = services.confirm_payment(payment, request.data or {})
        body = payment_payload(payment)
        return Response(body, status=status.HTTP_200_OK if payment.status == Payment.Status.SUCCEEDED else 402)


class RazorpayWebhookView(APIView):
    """Server-to-server confirmation, so a closed app never loses a payment."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        body = request.body
        try:
            event = json.loads(body)
            entity = event["payload"]["payment"]["entity"]
            order_id = entity["order_id"]
        except (ValueError, KeyError, TypeError):
            return Response(status=status.HTTP_400_BAD_REQUEST)
        with unscoped():
            payment = Payment.all_objects.select_related("invoice", "invoice__school").filter(gateway_order_id=order_id).first()
        if payment is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        school = payment.invoice.school
        secret = ((school.settings.get("payments") or {}).get("razorpay") or {}).get("webhook_secret", "")
        signature = request.META.get("HTTP_X_RAZORPAY_SIGNATURE", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, signature):
            return Response(status=status.HTTP_403_FORBIDDEN)
        if event.get("event") == "payment.captured":
            with use_school(school):
                # Webhook authenticity is proven by the signature; mark as paid.
                payment.gateway_payment_id = entity.get("id", "")
                _confirm_from_webhook(payment)
        return Response({"ok": True})


def _confirm_from_webhook(payment: Payment) -> None:
    from django.db import transaction
    from django.utils import timezone

    from .models import FeeInvoice as Invoice

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status == Payment.Status.SUCCEEDED:
            return
        locked.status = Payment.Status.SUCCEEDED
        locked.gateway_payment_id = payment.gateway_payment_id
        locked.paid_at = timezone.now()
        locked.receipt_no = services.next_receipt_no(payment.invoice.school, school_today(payment.invoice.school))
        locked.save()
        invoice = Invoice.objects.select_for_update().get(pk=locked.invoice_id)
        invoice.paid_amount = min(invoice.amount, invoice.paid_amount + locked.amount)
        invoice.save(update_fields=["paid_amount", "updated_at"])


class RefundRequestView(SchoolAPIView):
    """Accounts (or the office) raise a refund against a payment; the principal approves it."""

    allowed_roles = frozenset({Role.ACCOUNTANT, Role.PRINCIPAL, Role.ADMIN})

    def post(self, request, payment_id):
        from rest_framework.exceptions import ValidationError

        from apps.approvals.services import open_request
        from apps.accounts.models import User

        from .models import Refund

        payment = Payment.objects.filter(id=payment_id, status=Payment.Status.SUCCEEDED).select_related("invoice__student").first()
        if payment is None:
            raise Http404
        try:
            amount = Decimal(str(request.data.get("amount")))
        except Exception as exc:
            raise ValidationError({"amount": "Enter the amount."}) from exc
        already = sum((r.amount for r in payment.refunds.exclude(status="declined")), Decimal("0"))
        if amount <= 0 or amount + already > payment.amount:
            raise ValidationError({"amount": f"Up to ₹{payment.amount - already:,.0f} can be refunded on this receipt."})
        reason = str(request.data.get("reason", "")).strip()
        if not reason:
            raise ValidationError({"reason": "Add the reason."})
        asked_by = User.objects.filter(id=request.data.get("asked_by")).first() if request.data.get("asked_by") else payment.paid_by
        refund = Refund.objects.create(payment=payment, amount=amount, reason=reason[:400], fee_head=str(request.data.get("fee_head", ""))[:60], asked_by=asked_by)
        student = payment.invoice.student
        open_request(kind="refund", target=refund, requested_by=request.user, summary=f"{student.full_name} · {student.class_group.short_label} · ₹{amount:,.0f}")
        return Response({"id": str(refund.id), "status": refund.status}, status=status.HTTP_201_CREATED)
