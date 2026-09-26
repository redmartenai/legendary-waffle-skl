"""Console operations: Fees & Finance (collection, overdue ledger, reminders, calls, export, receipts)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.academics.models import Student
from apps.accounts.models import AuditLog
from apps.core.utils import school_today
from apps.fees.models import FeeInvoice, FeeReminder, InstalmentPlan, Payment
from apps.notifications.models import Notification

from .conftest import PARENT_MEERA, PARENT_RAHUL, TEACHER_ANITA, api, user


@pytest.fixture
def overdue(ghis, in_ghis):
    """Kabir (Rahul's son) owes ₹20,000 due 40 days ago; Aarav (Meera's) has ₹30,000 on an instalment plan."""
    today = school_today(ghis)
    kabir = Student.objects.get(full_name="Kabir Sharma")
    aarav = Student.objects.get(full_name="Aarav Iyer")
    FeeInvoice.objects.filter(student__in=[kabir, aarav], paid_amount=0).update(due_date=today + timedelta(days=20))
    inv = FeeInvoice.objects.create(student=kabir, title="Activity fee", category="activity", amount=Decimal(20000), due_date=today - timedelta(days=40))
    planned = FeeInvoice.objects.create(student=aarav, title="Lab fee", category="other", amount=Decimal(30000), paid_amount=Decimal(10000), due_date=today - timedelta(days=30))
    Payment.objects.create(invoice=planned, amount=Decimal(10000), gateway="counter", method="cash", status="succeeded", paid_at=timezone.now(), receipt_no="GHIS/T/1")
    InstalmentPlan.objects.create(invoice=planned, instalments=[{"due_on": (today - timedelta(days=30)).isoformat(), "amount": "10000"}, {"due_on": (today + timedelta(days=30)).isoformat(), "amount": "20000"}])
    return {"kabir": kabir, "invoice": inv, "planned": planned, "rahul": user(PARENT_RAHUL)}


def test_fees_page(principal, overdue):
    body = principal.get("/api/v1/console/fees", {"period": "year"}).json()
    assert body["period"]["key"] == "year"
    assert Decimal(body["billed"]) >= Decimal(body["collected"]) > 0
    # Only Kabir's family is overdue; the plan's balance isn't overdue because its instalments are on time.
    assert body["overdue"] == {"amount": "20000", "students": 1, "families": 1, "oldest_days": 40}
    assert body["on_plan"] == "20000"
    fam = body["ledger"]["preview"][0]
    assert fam["guardian"]["id"] == str(overdue["rahul"].id)
    assert fam["children"][0]["name"] == "Kabir Sharma" and fam["last_reminder"] is None
    assert body["ledger"]["counts"] == {"all": 1, "over60": 0, "never": 1, "students": 1}
    assert body["receipts_today"]["count"] >= 1
    assert [m["month"] for m in body["months"]][0] <= "2026-04"
    assert principal.get("/api/v1/console/fees", {"period": "term9"}).status_code == 400


def test_overdue_ledger_filters(principal, overdue):
    rows = principal.get("/api/v1/console/fees/overdue", {"period": "year", "q": "kabir"}).json()
    assert rows["total"] == 1 and rows["amount"] == "20000"
    assert principal.get("/api/v1/console/fees/overdue", {"period": "year", "segment": "over60"}).json()["total"] == 0
    assert principal.get("/api/v1/console/fees/overdue", {"period": "year", "q": "nobody"}).json()["total"] == 0
    assert principal.get("/api/v1/console/fees/overdue", {"period": "year", "segment": "bogus"}).status_code == 400


def test_send_reminders(principal, overdue):
    fam_id = str(overdue["rahul"].id)
    res = principal.post("/api/v1/console/fees/reminders", {"period": "year", "family_ids": [fam_id], "channels": ["app", "sms", "whatsapp"]}, format="json")
    assert res.status_code == 201, res.json()
    assert res.json()["families"] == 1 and res.json()["amount"] == "20000"
    r = FeeReminder.objects.get(guardian=overdue["rahul"])
    assert r.channels == ["app", "sms", "whatsapp"] and r.deliveries["sms"]["status"] == "logged"
    assert Notification.objects.filter(user=overdue["rahul"], category="fees", title__startswith="Fee reminder").exists()
    assert AuditLog.objects.filter(action="fees.remind").exists()
    # The ledger now shows it as the last reminder, and the family is no longer "never reminded".
    fam = principal.get("/api/v1/console/fees/overdue", {"period": "year"}).json()["items"][0]
    assert fam["last_reminder"]["channels"] == ["app", "sms", "whatsapp"]
    assert principal.get("/api/v1/console/fees/overdue", {"period": "year", "segment": "never"}).json()["total"] == 0
    # Everyone in a segment at once.
    assert principal.post("/api/v1/console/fees/reminders", {"period": "year", "all": True, "channels": ["app"]}, format="json").json()["families"] == 1


def test_reminder_validation(principal, overdue):
    post = lambda body: principal.post("/api/v1/console/fees/reminders", {"period": "year", **body}, format="json")  # noqa: E731
    assert post({"family_ids": []}).status_code == 400
    assert post({"family_ids": [str(overdue["rahul"].id)], "channels": ["pigeon"]}).status_code == 400
    assert post({"family_ids": [str(user(PARENT_MEERA).id)]}).json()["error"]["fields"]["family_ids"]
    assert post({"all": True, "segment": "never", "q": "nobody"}).status_code == 400


def test_log_call(principal, overdue):
    res = principal.post("/api/v1/console/fees/calls", {"period": "year", "guardian_id": str(overdue["rahul"].id), "status": "answered", "note": "Promised by Friday"}, format="json")
    assert res.status_code == 201 and res.json()["channels"] == ["call"] and res.json()["note"] == "Promised by Friday"
    assert AuditLog.objects.filter(action="fees.call").exists()
    assert principal.post("/api/v1/console/fees/calls", {"period": "year", "guardian_id": str(overdue["rahul"].id), "status": "maybe"}, format="json").status_code == 400
    assert principal.post("/api/v1/console/fees/calls", {"period": "year", "guardian_id": str(user(PARENT_MEERA).id), "status": "answered"}, format="json").status_code == 400


def test_export_is_audited(principal, overdue):
    res = principal.get("/api/v1/console/fees/export", {"period": "year"})
    assert res.status_code == 200 and res["Content-Type"].startswith("text/csv")
    text = res.content.decode()
    assert text.splitlines()[0].startswith("Admission no,Student,Class")
    assert "Kabir Sharma" in text and "Lab fee" in text
    assert AuditLog.objects.filter(action="fees.export").exists()


def test_receipts(principal, overdue):
    today = principal.get("/api/v1/console/fees/receipts").json()
    assert today["count"] >= 1
    rid = today["items"][0]["id"]
    one = principal.get(f"/api/v1/console/fees/receipts/{rid}").json()
    assert one["receipt_no"] and one["pdf"] == f"/fees/payments/{rid}/receipt.pdf" and one["invoice"]["title"]
    assert principal.get("/api/v1/console/fees/receipts/00000000-0000-0000-0000-000000000000").status_code == 404
    assert principal.get("/api/v1/console/fees/receipts", {"date": "yesterday"}).status_code == 400


@pytest.mark.parametrize("phone", [TEACHER_ANITA, PARENT_MEERA])
def test_roles_denied(ghis, overdue, phone):
    client = api(phone, ghis)
    assert client.get("/api/v1/console/fees").status_code == 403
    assert client.get("/api/v1/console/fees/overdue").status_code == 403
    assert client.post("/api/v1/console/fees/reminders", {"all": True}, format="json").status_code == 403
    assert client.get("/api/v1/console/fees/export").status_code == 403

