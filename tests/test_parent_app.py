"""The Parent app additions: day ribbon, remarks acks, homework sign-off, leave, documents and PDFs,
fee items, chat meetings and notification preferences."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.academics.models import Remark, Student
from apps.announcements.models import Announcement
from apps.core.utils import school_today
from apps.fees.models import FeeInvoice, FeeInvoiceItem, Payment
from apps.homework.models import Homework
from apps.messaging.models import Conversation, ConversationMember

from .conftest import PARENT_MEERA, PARENT_RAHUL, STUDENT_KABIR, TEACHER_ANITA, api, user


def _student(name):
    return Student.objects.get(full_name=name)


@pytest.mark.django_db
def test_summary_has_today_invoice_and_event(parent, in_ghis):
    if timezone.localdate().weekday() == 6:
        pytest.skip("No periods on Sunday")
    aarav = _student("Aarav Iyer")
    invoice = FeeInvoice.objects.filter(student=aarav, paid_amount=0).order_by("due_date").first()
    FeeInvoiceItem.objects.create(invoice=invoice, head="Tuition", amount=invoice.amount)
    Announcement.objects.create(
        title="Parent–teacher meeting", body="Book a slot.", kind=Announcement.Kind.EVENT,
        audience=Announcement.Audience.FAMILIES, event_starts_at=timezone.now() + timedelta(days=3), published_at=timezone.now(),
    )
    body = parent.get(f"/api/v1/students/{aarav.id}/summary").json()
    assert body["today"]["ribbon"]["cells"], "periods for today"
    assert all(c["kind"] in ("period", "break") for c in body["today"]["ribbon"]["cells"])
    assert body["next_invoice"]["heads"] == ["Tuition"]
    assert body["next_event"]["title"] == "Parent–teacher meeting"
    assert body["latest_remark"]["author_initials"] == "AR"


@pytest.mark.django_db
def test_children_detail(parent):
    body = parent.get("/api/v1/parent/children", {"detail": "1"}).json()
    aarav = next(c for c in body["children"] if c["name"] == "Aarav Iyer")
    assert aarav["class_teacher"] == "Anita Rao"
    assert aarav["transport"]["mode"] == "bus"
    assert aarav["month"]["school_days"] >= 1
    assert aarav["latest_exam"]["name"] == "Term 1"


@pytest.mark.django_db
def test_family_cannot_see_staff_only_remarks_and_can_acknowledge(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    hidden = Remark.objects.create(student=aarav, author=user(TEACHER_ANITA), body="Staff note", tone="info", visibility="staff")
    concern = Remark.objects.create(student=aarav, author=user(TEACHER_ANITA), body="Please check the lab record", tone="concern", requires_ack=True)
    items = parent.get(f"/api/v1/students/{aarav.id}/remarks").json()["items"]
    ids = {r["id"] for r in items}
    assert str(hidden.id) not in ids and str(concern.id) in ids
    assert parent.post(f"/api/v1/remarks/{concern.id}/ack").status_code == 200
    items = parent.get(f"/api/v1/students/{aarav.id}/remarks").json()["items"]
    assert next(r for r in items if r["id"] == str(concern.id))["acknowledged_at"]
    # Staff still see their note, and a family can't ack a staff-only remark.
    assert str(hidden.id) in {r["id"] for r in api(TEACHER_ANITA).get(f"/api/v1/students/{aarav.id}/remarks").json()["items"]}
    assert parent.post(f"/api/v1/remarks/{hidden.id}/ack").status_code == 404


@pytest.mark.django_db
def test_homework_signoff_and_week(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    homework = Homework.objects.filter(class_group=aarav.class_group).order_by("-due_date").first()
    response = parent.post(f"/api/v1/homework/{homework.id}/signoff", {"student_id": str(aarav.id)}, format="json")
    assert response.status_code == 200
    week = parent.get(f"/api/v1/students/{aarav.id}/homework", {"week": homework.due_date.isoformat()}).json()
    item = next(h for h in week["items"] if h["id"] == str(homework.id))
    assert item["signed"]["by"] == "Meera Iyer"
    assert all(h["due_date"][:7] for h in week["items"])
    # Another family's parent can't sign it.
    assert api(PARENT_RAHUL).post(f"/api/v1/homework/{homework.id}/signoff", {"student_id": str(aarav.id)}, format="json").status_code == 404


@pytest.mark.django_db
def test_leave_application(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    today = school_today(aarav.school)
    body = {"from_date": (today + timedelta(days=2)).isoformat(), "to_date": (today + timedelta(days=3)).isoformat(), "kind": "family", "reason": "Cousin's wedding"}
    created = parent.post(f"/api/v1/students/{aarav.id}/leave", body, format="json")
    assert created.status_code == 201 and created.json()["status"] == "pending"
    assert parent.get(f"/api/v1/students/{aarav.id}/leave").json()["items"][0]["reason"] == "Cousin's wedding"
    bad = parent.post(f"/api/v1/students/{aarav.id}/leave", {**body, "to_date": today.isoformat()}, format="json")
    assert bad.status_code == 400
    # Teachers don't apply for a child's leave.
    assert api(TEACHER_ANITA).post(f"/api/v1/students/{aarav.id}/leave", body, format="json").status_code == 403


@pytest.mark.django_db
def test_attendance_month_has_year_summary(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    body = parent.get(f"/api/v1/students/{aarav.id}/attendance").json()
    assert body["year"]["school_days"] >= body["summary"]["school_days"]
    assert "note" in body["days"][0] and "leave" in body["days"][0]


@pytest.mark.django_db
def test_documents_report_card_and_receipt_pdfs(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    docs = parent.get(f"/api/v1/students/{aarav.id}/documents").json()
    assert docs["report_cards"] and docs["receipts"]
    assert {c["kind"]: c["state"] for c in docs["certificates"]}["transfer"] == "locked"
    report = parent.get("/api/v1" + docs["report_cards"][0]["download"])
    assert report.status_code == 200 and report["Content-Type"] == "application/pdf" and report.content[:4] == b"%PDF"
    receipt = parent.get("/api/v1" + docs["receipts"][0]["download"])
    assert receipt.status_code == 200 and receipt.content[:4] == b"%PDF"
    # Another family can't download them.
    assert api(PARENT_RAHUL).get("/api/v1" + docs["receipts"][0]["download"]).status_code == 404


@pytest.mark.django_db
def test_certificate_request_once(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    first = parent.post(f"/api/v1/students/{aarav.id}/certificates", {"kind": "bonafide", "purpose": "Passport"}, format="json")
    assert first.status_code == 201
    again = parent.post(f"/api/v1/students/{aarav.id}/certificates", {"kind": "bonafide"}, format="json")
    assert again.status_code == 400
    assert parent.post(f"/api/v1/students/{aarav.id}/certificates", {"kind": "transfer"}, format="json").status_code == 400


@pytest.mark.django_db
def test_fee_items_and_year(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    invoice = FeeInvoice.objects.filter(student=aarav).first()
    FeeInvoiceItem.objects.create(invoice=invoice, head="Lab", amount=1000)
    body = parent.get(f"/api/v1/students/{aarav.id}/fees").json()
    assert body["year"]["total"]
    assert next(i for i in body["invoices"] if i["id"] == str(invoice.id))["items"][0]["head"] == "Lab"
    assert all("download" in r for r in body["receipts"])
    pdf = parent.get(f"/api/v1/fees/invoices/{invoice.id}/invoice.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert api(PARENT_RAHUL).get(f"/api/v1/fees/invoices/{invoice.id}/invoice.pdf").status_code == 404


@pytest.mark.django_db
def test_meeting_booked_by_staff_shows_in_thread_with_calendar(in_ghis):
    conversation = Conversation.objects.filter(members__user__phone=PARENT_MEERA, kind="direct").first()
    staff = ConversationMember.objects.filter(conversation=conversation, side="staff").first().user
    start = timezone.now() + timedelta(days=5)
    body = {"title": "PTM", "starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=15)).isoformat(), "location": "Room 214"}
    parent = api(PARENT_MEERA)
    assert parent.post(f"/api/v1/chat/conversations/{conversation.id}/meetings", body, format="json").status_code == 403
    assert api(staff.phone).post(f"/api/v1/chat/conversations/{conversation.id}/meetings", body, format="json").status_code == 201
    thread = parent.get(f"/api/v1/chat/conversations/{conversation.id}/messages").json()
    meeting = thread["meetings"][0]
    ics = parent.get("/api/v1" + meeting["calendar"])
    assert ics.status_code == 200 and b"BEGIN:VEVENT" in ics.content


@pytest.mark.django_db
def test_notification_preferences(parent):
    me = parent.get("/api/v1/me").json()["user"]
    assert me["preferences"]["channels"]["push"] is True
    updated = parent.patch("/api/v1/me", {"preferences": {"channels": {"whatsapp": True}}}, format="json").json()["user"]
    assert updated["preferences"]["channels"]["whatsapp"] is True and updated["preferences"]["alerts"]["late_arrival"] is True
    assert parent.patch("/api/v1/me", {"preferences": {"channels": {"fax": True}}}, format="json").status_code == 400


@pytest.mark.django_db
def test_student_cannot_sign_homework(in_ghis):
    kabir = _student("Kabir Sharma")
    homework = Homework.objects.filter(class_group=kabir.class_group).first()
    response = api(STUDENT_KABIR).post(f"/api/v1/homework/{homework.id}/signoff", {"student_id": str(kabir.id)}, format="json")
    assert response.status_code == 403
    _ = Payment
