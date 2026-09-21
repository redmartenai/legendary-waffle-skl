from decimal import Decimal

import pytest

from apps.academics.models import Student
from apps.fees.models import FeeInvoice, Payment
from apps.fees.services import financial_year, format_inr


@pytest.mark.django_db
def test_checkout_and_confirm_issues_gapless_receipts(ghis, in_ghis, parent):
    aarav = Student.objects.get(full_name="Aarav Iyer")
    fees = parent.get(f"/api/v1/students/{aarav.id}/fees").json()
    due = [i for i in fees["invoices"] if i["status"] in {"due", "overdue"}]
    assert len(due) == 2

    receipts = []
    for invoice in due:
        checkout = parent.post(f"/api/v1/fees/invoices/{invoice['id']}/checkout", {}, format="json", HTTP_IDEMPOTENCY_KEY=f"k-{invoice['id']}")
        assert checkout.status_code == 201
        again = parent.post(f"/api/v1/fees/invoices/{invoice['id']}/checkout", {}, format="json", HTTP_IDEMPOTENCY_KEY=f"k-{invoice['id']}")
        assert again.json()["payment_id"] == checkout.json()["payment_id"]  # no double charge
        confirmed = parent.post(f"/api/v1/fees/payments/{checkout.json()['payment_id']}/confirm", {"simulate": "success"}, format="json")
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "succeeded"
        receipts.append(confirmed.json()["receipt_no"])

    numbers = sorted(int(r.rsplit("/", 1)[1]) for r in receipts)
    assert numbers[1] == numbers[0] + 1  # gapless
    assert parent.get(f"/api/v1/students/{aarav.id}/fees").json()["total_due"] == "0.00"


@pytest.mark.django_db
def test_failed_payment_leaves_invoice_due(ghis, in_ghis, parent):
    diya = Student.objects.get(full_name="Diya Iyer")
    invoice = FeeInvoice.objects.filter(student=diya, paid_amount=0).first()
    checkout = parent.post(f"/api/v1/fees/invoices/{invoice.id}/checkout", {}, format="json").json()
    failed = parent.post(f"/api/v1/fees/payments/{checkout['payment_id']}/confirm", {"simulate": "failure"}, format="json")
    assert failed.status_code == 402
    invoice.refresh_from_db()
    assert invoice.paid_amount == 0
    assert Payment.objects.get(id=checkout["payment_id"]).receipt_no is None


def test_financial_year_and_indian_number_format():
    from datetime import date

    assert financial_year(date(2026, 9, 19)) == "2026-27"
    assert financial_year(date(2027, 2, 1)) == "2026-27"
    assert format_inr(Decimal("99500")) == "99,500"
    assert format_inr(Decimal("1234567")) == "12,34,567"
