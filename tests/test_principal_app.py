"""Principal app: approvals (decide, undo, audit), marks corrections, refunds, cover, pulse, broadcast channels."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.academics.models import ClassGroup, Student, Subject, TimetableSlot
from apps.announcements.delivery import deliver_due
from apps.announcements.models import Announcement, ChannelDelivery
from apps.approvals.models import ApprovalEvent, ApprovalRequest
from apps.fees.models import FeeInvoice, Payment
from apps.results.models import Exam, ExamMark, MarkSheet
from apps.staff.models import StaffLeave

from .conftest import PARENT_MEERA, PRINCIPAL, STUDENT_KABIR, TEACHER_ANITA, TEACHER_VIKRAM, api, user


@pytest.fixture
def principal(in_ghis):
    return api(PRINCIPAL)


def _next_weekday(days: int = 7) -> date:
    d = date.today() + timedelta(days=days)
    return d + timedelta(days=1) if d.weekday() == 6 else d


@pytest.mark.django_db
def test_leave_goes_through_the_in_tray_with_undo_and_audit(principal):
    day = _next_weekday()
    made = api(TEACHER_ANITA).post("/api/v1/staff/leave", {"kind": "casual", "from_date": day.isoformat(), "reason": "Wedding"}, format="json")
    assert made.status_code == 201
    tray = principal.get("/api/v1/approvals").json()
    assert tray["counts"]["leave"] == 1
    item = tray["items"][0]
    assert item["details"]["person"]["name"] == "Anita Rao" and item["details"]["left_after"] == 11
    done = principal.post(f"/api/v1/approvals/{item['id']}/decide", {"decision": "approve"}, format="json").json()
    assert done["status"] == "approved" and done["undo_until"]
    assert StaffLeave.objects.get(id=made.json()["id"]).status == "approved"
    # Undo puts it back in the tray and the leave back to pending.
    principal.post(f"/api/v1/approvals/{item['id']}/undo")
    assert StaffLeave.objects.get(id=made.json()["id"]).status == "pending"
    principal.post(f"/api/v1/approvals/{item['id']}/decide", {"decision": "decline", "note": "Exam week"}, format="json")
    assert StaffLeave.objects.get(id=made.json()["id"]).status == "declined"
    actions = list(ApprovalEvent.objects.filter(request_id=item["id"]).values_list("action", flat=True))
    assert actions == ["submitted", "approved", "undone", "declined"]
    # A decided request can't be decided again; teachers can't see the tray.
    assert principal.post(f"/api/v1/approvals/{item['id']}/decide", {"decision": "approve"}, format="json").status_code == 400
    assert api(TEACHER_ANITA).get("/api/v1/approvals").status_code == 403


@pytest.mark.django_db
def test_withdrawn_leave_leaves_the_tray(principal):
    made = api(TEACHER_ANITA).post("/api/v1/staff/leave", {"kind": "casual", "from_date": _next_weekday().isoformat(), "reason": "x"}, format="json").json()
    api(TEACHER_ANITA).post(f"/api/v1/staff/leave/{made['id']}/cancel")
    assert principal.get("/api/v1/approvals").json()["total"] == 0


@pytest.mark.django_db
def test_marks_correction_after_publishing(principal, in_ghis):
    group = ClassGroup.objects.get(grade="10", section="C")
    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    maths = Subject.objects.get(code="MATH")
    exam = Exam.objects.create(class_group=group, name="UT9", held_on=date.today(), is_published=True)
    sheet = MarkSheet.objects.create(exam=exam, subject=maths, max_marks=40)
    ExamMark.objects.create(exam=exam, student=kabir, subject=maths, marks=28, max_marks=40)
    anita = api(TEACHER_ANITA)
    # Published marks can't be edited directly...
    assert anita.put(f"/api/v1/marksheets/{sheet.id}", {"entries": []}, format="json").status_code == 403
    # ...so the teacher asks for a correction.
    sent = anita.post(f"/api/v1/marksheets/{sheet.id}/corrections", {"reason": "Key error", "entries": [{"student_id": str(kabir.id), "marks": 32}]}, format="json")
    assert sent.status_code == 202
    item = principal.get("/api/v1/approvals", {"kind": "marks"}).json()["items"][0]
    assert item["details"]["entries"][0]["from"] == 28 and item["details"]["entries"][0]["to"] == 32
    principal.post(f"/api/v1/approvals/{item['id']}/decide", {"decision": "approve"}, format="json")
    assert ExamMark.objects.get(exam=exam, student=kabir).marks == 32
    principal.post(f"/api/v1/approvals/{item['id']}/undo")
    assert ExamMark.objects.get(exam=exam, student=kabir).marks == 28
    # Sending back needs a note.
    assert principal.post(f"/api/v1/approvals/{item['id']}/decide", {"decision": "send_back"}, format="json").status_code == 400


@pytest.mark.django_db
def test_refund_request_and_approval(principal, in_ghis):
    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    invoice = FeeInvoice.objects.create(student=kabir, title="Transport Q3", category="transport", amount=Decimal(6000), due_date=date.today(), paid_amount=Decimal(6000))
    payment = Payment.objects.create(invoice=invoice, amount=Decimal(6000), gateway="mock", status="succeeded", paid_at=timezone.now())
    assert principal.post(f"/api/v1/fees/payments/{payment.id}/refunds", {"amount": 7000, "reason": "x"}, format="json").status_code == 400
    made = principal.post(f"/api/v1/fees/payments/{payment.id}/refunds", {"amount": 6000, "reason": "Moved off the route"}, format="json")
    assert made.status_code == 201
    item = principal.get("/api/v1/approvals", {"kind": "refund"}).json()["items"][0]
    assert item["details"]["amount"] == "6000.00"
    assert principal.post(f"/api/v1/approvals/{item['id']}/decide", {"decision": "approve"}, format="json").json()["status"] == "approved"
    assert api(PARENT_MEERA).post(f"/api/v1/fees/payments/{payment.id}/refunds", {"amount": 1, "reason": "x"}, format="json").status_code == 403


@pytest.mark.django_db
def test_cover_assignment(principal, in_ghis):
    today = date.today()
    if today.weekday() == 6:
        pytest.skip("No school on Sunday")
    vikram = user(TEACHER_VIKRAM)
    StaffLeave.objects.create(user=vikram, kind="sick", from_date=today, to_date=today, days=1, reason="Fever", status="approved")
    board = principal.get("/api/v1/principal/cover").json()
    assert board["on_leave"][0]["name"] == "Vikram Das"
    slot = TimetableSlot.objects.filter(teacher=vikram, weekday=today.weekday()).first()
    anita = user(TEACHER_ANITA)
    busy = TimetableSlot.objects.filter(teacher=anita, weekday=today.weekday(), period=slot.period).exists()
    made = principal.post("/api/v1/principal/cover", {"slot_id": str(slot.id), "teacher_id": str(anita.id)}, format="json")
    if busy:
        assert made.status_code == 400
    else:
        assert made.status_code == 201
        assert principal.post("/api/v1/principal/cover", {"slot_id": str(slot.id), "teacher_id": str(anita.id)}, format="json").status_code == 400


@pytest.mark.django_db
def test_pulse_and_attendance(principal):
    pulse = principal.get("/api/v1/principal/pulse")
    assert pulse.status_code == 200 and "register" in pulse.json()
    att = principal.get("/api/v1/principal/attendance").json()
    assert att["staff"]["teachers"] >= 3
    assert api(TEACHER_ANITA).get("/api/v1/principal/pulse").status_code == 403


@pytest.mark.django_db
def test_broadcast_estimate_channels_and_schedule(principal, in_ghis):
    est = principal.post("/api/v1/announcements/estimate", {"audience": "everyone"}, format="json").json()
    assert est["families"] == Student.objects.filter(is_active=True).count() and est["staff"] >= 3
    grade = principal.post("/api/v1/announcements/estimate", {"audience": "families", "grades": ["10"]}, format="json").json()
    assert grade["families"] == Student.objects.filter(class_group__grade="10", is_active=True).count()
    sent = principal.post(
        "/api/v1/announcements",
        {"title": "PTM", "body": "Saturday 9 AM", "audience": "families", "channels": ["push", "in_app", "sms", "email"]},
        format="json",
    )
    assert sent.status_code == 201
    assert ChannelDelivery.objects.filter(announcement_id=sent.json()["id"], channel="sms").exists()
    # A scheduled notice is invisible to families until it goes out.
    later = (timezone.now() + timedelta(hours=2)).isoformat()
    sched = principal.post("/api/v1/announcements", {"title": "Holiday", "body": "Monday off", "audience": "families", "scheduled_at": later}, format="json").json()
    assert sched["scheduled"] is True
    assert sched["id"] not in [a["id"] for a in api(PARENT_MEERA).get("/api/v1/announcements").json()["items"]]
    Announcement.objects.filter(id=sched["id"]).update(published_at=timezone.now() - timedelta(minutes=1))
    assert deliver_due() == 1
    assert sched["id"] in [a["id"] for a in api(PARENT_MEERA).get("/api/v1/announcements").json()["items"]]
    # Teachers can't send SMS.
    group = ClassGroup.objects.get(grade="10", section="C")
    teacher = api(TEACHER_ANITA).post("/api/v1/announcements", {"title": "x", "body": "y", "audience": "classes", "class_ids": [str(group.id)], "channels": ["sms"]}, format="json")
    assert teacher.status_code == 403
    _ = ApprovalRequest


@pytest.mark.django_db
def test_route_broadcast_reaches_only_that_route(principal, in_ghis):
    from apps.transport.models import StudentTransport

    ride = StudentTransport.objects.filter(is_active=True).select_related("route").first()
    riders = StudentTransport.objects.filter(route=ride.route, is_active=True).count()
    est = principal.post("/api/v1/announcements/estimate", {"audience": "route", "route_id": str(ride.route_id)}, format="json").json()
    assert est["families"] == riders and est["staff"] == 0
    made = principal.post("/api/v1/announcements", {"title": "Late", "body": "12 min late", "audience": "route", "route_id": str(ride.route_id)}, format="json").json()
    assert made["audience_label"].startswith("Families on")
    # Meera's children ride Route 4, so she sees it; a family off the route wouldn't.
    seen = [a["id"] for a in api(PARENT_MEERA).get("/api/v1/announcements").json()["items"]]
    meera_rides = StudentTransport.objects.filter(route=ride.route, student__guardian_links__user=user(PARENT_MEERA)).exists()
    assert (made["id"] in seen) == meera_rides
