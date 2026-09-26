"""Console: approvals (the web in-tray, decisions with audit, history, evidence files, SLA rules)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from apps.accounts.models import AuditLog
from apps.academics.models import ClassGroup, Student, Subject
from apps.approvals.models import ApprovalAttachment, ApprovalEvent, ApprovalRequest
from apps.approvals.services import open_request
from apps.approvals.sla import sla_state
from apps.results.models import Exam, ExamMark, MarkCorrection

from .conftest import PARENT_MEERA, PRINCIPAL, STUDENT_KABIR, TEACHER_ANITA, TEACHER_VIKRAM, api, user

BASE = "/api/v1/console/approvals"


@pytest.fixture
def principal(in_ghis):
    return api(PRINCIPAL)


def _next_weekday(days: int = 7) -> date:
    d = date.today() + timedelta(days=days)
    return d + timedelta(days=1) if d.weekday() == 6 else d


def _leave(days=7) -> ApprovalRequest:
    made = api(TEACHER_ANITA).post("/api/v1/staff/leave", {"kind": "casual", "from_date": _next_weekday(days).isoformat(), "reason": "Wedding"}, format="json")
    assert made.status_code == 201
    return ApprovalRequest.objects.get(target_id=made.json()["id"])


def _marks() -> ApprovalRequest:
    """A published marks correction the HOD has already checked."""
    group = ClassGroup.objects.get(grade="10", section="C")
    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    science = Subject.objects.get(code="MATH")
    exam = Exam.objects.create(class_group=group, name="UT9", held_on=date.today(), is_published=True)
    ExamMark.objects.create(exam=exam, student=kabir, subject=science, marks=28, max_marks=40)
    corr = MarkCorrection.objects.create(
        exam=exam, subject=science, reason="Key error on Q7", checked_by=user(TEACHER_VIKRAM),
        entries=[{"student_id": str(kabir.id), "from": 28.0, "to": 32.0, "note": "Q7 · 0 → 4"}],
    )
    return open_request(kind="marks", target=corr, requested_by=user(TEACHER_ANITA), summary="UT9 · 10-C Maths · 1 change", notify_principal=False)


@pytest.mark.django_db
def test_intray_lists_pending_with_sla_trail_and_counts(principal):
    leave = _leave(days=1)
    marks = _marks()
    ApprovalRequest.objects.filter(pk=marks.pk).update(created_at=timezone.now() - timedelta(hours=60))
    data = principal.get(BASE).json()
    assert data["total"] == 2 and data["counts"]["leave"] == 1 and data["counts"]["marks"] == 1
    assert data["past_sla"] == 1
    by_id = {i["id"]: i for i in data["items"]}
    m = by_id[str(marks.id)]
    assert m["sla"]["past"] and m["sla"]["past_by_hours"] >= 11
    # The HOD's check is part of the trail, and the marker's note names the question.
    assert [s["action"] for s in m["trail"]] == ["submitted", "checked"]
    assert m["trail"][1]["actor"]["name"] == user(TEACHER_VIKRAM).full_name
    entry = m["details"]["entries"][0]
    assert entry["question"] == "Q7" and entry["question_from"] == 0 and entry["question_to"] == 4
    assert m["details"]["class_size"] > 0
    lv = by_id[str(leave.id)]
    assert lv["sla"]["hours_left"] > 0 and not lv["sla"]["past"]
    # A leave starting on the next school day needs an answer today and is named in the lead sentence.
    if leave.due_on <= date.today() + timedelta(days=2):
        assert lv["decide_today"]
    assert data["decided_today"]["total"] == 0 and data["signer"]


@pytest.mark.django_db
def test_approve_writes_audit_shows_in_decided_today_and_undo(principal):
    req = _leave()
    done = principal.post(f"{BASE}/{req.id}/decide", {"decision": "approve", "note": ""}, format="json")
    assert done.status_code == 200 and done.json()["status"] == "approved" and done.json()["undo_until"]
    assert AuditLog.objects.filter(action="approvals.approve", target_id=str(req.id)).exists()
    data = principal.get(BASE).json()
    assert data["total"] == 0 and data["decided_today"]["total"] == 1
    assert principal.post(f"{BASE}/{req.id}/decide", {"decision": "approve"}, format="json").status_code == 400
    back = principal.post(f"{BASE}/{req.id}/undo").json()
    assert back["status"] == "pending"
    assert AuditLog.objects.filter(action="approvals.undo", target_id=str(req.id)).exists()
    actions = list(ApprovalEvent.objects.filter(request=req).values_list("action", flat=True))
    assert actions == ["submitted", "approved", "undone"]


@pytest.mark.django_db
def test_send_back_and_reject_need_a_note(principal):
    req = _leave()
    for decision in ("send_back", "reject"):
        r = principal.post(f"{BASE}/{req.id}/decide", {"decision": decision, "note": "  "}, format="json")
        assert r.status_code == 400 and "note" in r.json()["error"]["fields"]
    assert principal.post(f"{BASE}/{req.id}/decide", {"decision": "maybe"}, format="json").status_code == 400
    r = principal.post(f"{BASE}/{req.id}/decide", {"decision": "reject", "note": "Exam week"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "declined" and r.json()["decision_note"] == "Exam week"
    assert AuditLog.objects.filter(action="approvals.reject", target_id=str(req.id)).exists()
    hist = principal.get(f"{BASE}/history", {"range": "today", "status": "declined"}).json()
    assert [i["id"] for i in hist["items"]] == [str(req.id)] and hist["counts"]["declined"] == 1
    assert principal.get(f"{BASE}/history", {"range": "year"}).status_code == 400


@pytest.mark.django_db
def test_marks_send_back_leaves_marks_alone(principal):
    req = _marks()
    r = principal.post(f"{BASE}/{req.id}/decide", {"decision": "send_back", "note": "Attach the scripts"}, format="json")
    assert r.json()["status"] == "sent_back"
    assert ExamMark.objects.get(exam=req.target.exam).marks == Decimal("28")
    assert r.json()["trail"][-1]["action"] == "sent_back"


@pytest.mark.django_db
def test_detail_and_evidence_download(principal):
    req = _marks()
    doc = ApprovalAttachment.objects.create(request=req, file=ContentFile(b"%PDF-1.4 key", name="key.pdf"), name="Q7 answer key", size=12)
    item = principal.get(f"{BASE}/{req.id}").json()
    assert item["attachments"][0]["name"] == "Q7 answer key" and item["attachments"][0]["type"] == "PDF"
    got = principal.get(item["attachments"][0]["url"].replace("/console", "/api/v1/console", 1))
    assert got.status_code == 200 and b"".join(got.streaming_content) == b"%PDF-1.4 key"
    assert AuditLog.objects.filter(action="approvals.download").exists()
    assert principal.get(f"{BASE}/{req.id}/files/{req.id}").status_code == 404
    assert principal.get(f"{BASE}/{doc.id}").status_code == 404
    doc.file.delete(save=False)


@pytest.mark.django_db
def test_rules_change_the_sla(principal):
    req = _leave()
    rules = principal.get(f"{BASE}/rules").json()
    assert rules["sla_hours"]["refund"] == 48 and rules["undo_minutes"] == 10
    r = principal.put(f"{BASE}/rules", {"sla_hours": {"leave": 24}}, format="json")
    assert r.status_code == 200 and r.json()["sla_hours"]["leave"] == 24 and r.json()["sla_hours"]["refund"] == 48
    req.school.refresh_from_db()
    assert sla_state(ApprovalRequest.objects.get(pk=req.pk), timezone.now())["sla_hours"] == 24
    assert AuditLog.objects.filter(action="approvals.rules").exists()
    assert principal.put(f"{BASE}/rules", {"sla_hours": {"leave": 50}}, format="json").status_code == 400
    assert principal.put(f"{BASE}/rules", {"sla_hours": {"parking": 24}}, format="json").status_code == 400
    assert principal.put(f"{BASE}/rules", {}, format="json").status_code == 400


@pytest.mark.django_db
def test_teachers_and_parents_are_refused(in_ghis):
    req = _leave()
    for phone in (TEACHER_ANITA, PARENT_MEERA):
        client = api(phone)
        assert client.get(BASE).status_code == 403
        assert client.get(f"{BASE}/{req.id}").status_code == 403
        assert client.get(f"{BASE}/history").status_code == 403
        assert client.get(f"{BASE}/rules").status_code == 403
        assert client.put(f"{BASE}/rules", {"sla_hours": {"leave": 24}}, format="json").status_code == 403
        assert client.post(f"{BASE}/{req.id}/decide", {"decision": "approve"}, format="json").status_code == 403
    assert ApprovalRequest.objects.get(pk=req.pk).status == "pending"
