"""Console engage pages: Communication, Documents, Reports & Analytics."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.academics.models import ClassGroup, StudentGuardian
from apps.accounts.models import AuditLog
from apps.announcements.models import Announcement, AnnouncementAck, AnnouncementRead
from apps.messaging.models import Conversation, ConversationMember, Meeting, Message

from .conftest import PARENT_MEERA, PRINCIPAL, TEACHER_ANITA, api, user


@pytest.fixture
def principal(in_ghis):
    return api(PRINCIPAL)


# ------------------------------------------------------------------ communication


@pytest.mark.django_db
def test_communication_page_and_role_denial(principal):
    page = principal.get("/api/v1/console/communication")
    assert page.status_code == 200
    body = page.json()
    assert {"counts", "recent", "circulars", "meetings", "drafts", "quiet_hours", "sms_credits", "grades", "sections"} <= set(body)
    assert api(TEACHER_ANITA).get("/api/v1/console/communication").status_code == 403
    assert api(PARENT_MEERA).post("/api/v1/console/communication/announcements", {"intent": "send"}, format="json").status_code == 403


@pytest.mark.django_db
def test_draft_then_send_to_grade_parents_with_reach_and_reads(principal):
    grade = ClassGroup.objects.order_by("grade").first().grade
    estimate = principal.post("/api/v1/console/communication/estimate", {"scope": "grades", "grades": [grade], "parents_only": True}, format="json").json()
    assert estimate["empty"] is False and estimate["parents"] >= 1 and "balance" in estimate["sms_credits"]

    draft = principal.post(
        "/api/v1/console/communication/announcements",
        {"intent": "draft", "title": "Sports day", "body": "Dear {parent_name}, see you there.", "scope": "grades", "grades": [grade], "parents_only": True, "channels": ["push", "sms"]},
        format="json",
    )
    assert draft.status_code == 201 and draft.json()["status"] == "draft"
    item = Announcement.objects.get(id=draft.json()["id"])
    assert item.status == "draft" and item.delivered_at is None and item.parents_only
    # Drafts never reach families.
    assert all(a["title"] != "Sports day" for a in api(PARENT_MEERA).get("/api/v1/announcements").json()["items"])
    assert principal.get("/api/v1/console/communication").json()["drafts"][0]["id"] == str(item.id)

    sent = principal.post(
        "/api/v1/console/communication/announcements",
        {"id": str(item.id), "intent": "send", "title": "Sports day", "body": "Dear {parent_name}, see you there.", "scope": "grades", "grades": [grade], "parents_only": True, "channels": ["push", "sms"]},
        format="json",
    )
    assert sent.status_code == 201
    item.refresh_from_db()
    assert item.status == "published" and item.delivered_at and item.recipients >= 1
    assert "in_app" in item.channels
    assert AuditLog.objects.filter(action="announcements.send", target_id=str(item.id)).exists()
    # Parents only: guardians get it, the students' own logins don't.
    guardian = StudentGuardian.objects.filter(student__class_group__grade=grade).select_related("user").first().user
    reader = api(guardian.phone)
    assert reader.post(f"/api/v1/announcements/{item.id}/read", {"channel": "sms"}, format="json").status_code == 200
    assert AnnouncementRead.objects.filter(announcement=item, user=guardian, channel="sms").exists()
    report = principal.get("/api/v1/console/communication/reports").json()["items"]
    row = next(r for r in report if r["id"] == str(item.id))
    assert row["read"] == 1 and row["audience"]["label"] == grade and row["audience"]["parents_only"] is True


@pytest.mark.django_db
def test_send_validation(principal):
    bad = principal.post("/api/v1/console/communication/announcements", {"intent": "send", "title": "", "body": "", "scope": "grades", "grades": []}, format="json")
    assert bad.status_code == 400
    fields = bad.json()["error"]["fields"]
    assert {"title", "body", "grades"} <= set(fields)
    past = (timezone.now() - timedelta(hours=2)).isoformat()
    late = principal.post("/api/v1/console/communication/announcements", {"intent": "send", "title": "x", "body": "y", "scope": "school", "scheduled_at": past}, format="json")
    assert late.status_code == 400 and "scheduled_at" in late.json()["error"]["fields"]
    circular = principal.post("/api/v1/console/communication/announcements", {"intent": "send", "title": "x", "body": "y", "scope": "school", "circular": True}, format="json")
    assert "ack_due_on" in circular.json()["error"]["fields"]


@pytest.mark.django_db
def test_scheduled_send_waits_and_circular_gets_a_number(principal):
    later = (timezone.now() + timedelta(days=1)).isoformat()
    due = (timezone.now() + timedelta(days=5)).date().isoformat()
    made = principal.post(
        "/api/v1/console/communication/announcements",
        {"intent": "send", "title": "Circular: uniform", "body": "Winter uniform from November.", "scope": "school", "parents_only": True, "scheduled_at": later, "circular": True, "ack_due_on": due},
        format="json",
    ).json()
    item = Announcement.objects.get(id=made["id"])
    assert item.delivered_at is None and item.circular_no >= 1 and item.requires_ack and item.audience == "parents"
    assert made["scheduled"] is True
    listed = principal.get("/api/v1/console/communication/circulars").json()["items"]
    assert any(c["id"] == str(item.id) and c["due_on"] == due for c in listed)


@pytest.mark.django_db
def test_circular_ack_rate_and_remind(principal):
    grade = ClassGroup.objects.order_by("grade").first().grade
    due = (timezone.now() + timedelta(days=3)).date().isoformat()
    made = principal.post(
        "/api/v1/console/communication/announcements",
        {"intent": "send", "title": "Trip consent", "body": "Please acknowledge.", "scope": "grades", "grades": [grade], "parents_only": True, "circular": True, "ack_due_on": due},
        format="json",
    ).json()
    item = Announcement.objects.get(id=made["id"])
    guardian = StudentGuardian.objects.filter(student__class_group__grade=grade).first().user
    api(guardian.phone).post(f"/api/v1/announcements/{item.id}/ack")
    row = next(c for c in principal.get("/api/v1/console/communication/circulars").json()["items"] if c["id"] == str(item.id))
    assert row["acknowledged"] == 1 and row["state"] == ("done" if item.recipients == 1 else "pending")
    reminded = principal.post(f"/api/v1/console/communication/circulars/{item.id}/remind").json()["reminded"]
    assert reminded == item.recipients - AnnouncementAck.objects.filter(announcement=item).count()
    assert AuditLog.objects.filter(action="announcements.remind").exists()


@pytest.mark.django_db
def test_meeting_requests_accept_and_propose(principal):
    parent = user(PARENT_MEERA)
    head = user(PRINCIPAL)
    link = StudentGuardian.objects.filter(user=parent).first()
    chat = Conversation.objects.create(kind="direct", student=link.student)
    ConversationMember.objects.create(conversation=chat, user=parent, side="family")
    ConversationMember.objects.create(conversation=chat, user=head, side="staff", label="Principal")
    Message.objects.create(conversation=chat, sender=parent, body="Can we meet?", client_id="t-1")
    starts = timezone.now() + timedelta(days=2)
    one = Meeting.objects.create(conversation=chat, title="Bus timings", starts_at=starts, ends_at=starts + timedelta(minutes=20), status="requested", booked_by=parent)
    two = Meeting.objects.create(conversation=chat, title="Fees", starts_at=starts + timedelta(hours=1), ends_at=starts + timedelta(hours=1, minutes=20), status="requested", booked_by=parent)

    page = principal.get("/api/v1/console/communication").json()
    assert page["counts"]["meetings"] == 2 and page["counts"]["messages"] >= 1
    assert page["meetings"][0]["parent"]["name"] == parent.full_name and page["meetings"][0]["topic"] == "Bus timings"

    assert principal.post(f"/api/v1/console/communication/meetings/{one.id}/accept").json()["status"] == "booked"
    assert principal.post(f"/api/v1/console/communication/meetings/{one.id}/accept").status_code == 400
    bad = principal.post(f"/api/v1/console/communication/meetings/{two.id}/propose", {"starts_at": (timezone.now() - timedelta(hours=1)).isoformat()}, format="json")
    assert bad.status_code == 400
    new_time = (starts + timedelta(days=1)).isoformat()
    moved = principal.post(f"/api/v1/console/communication/meetings/{two.id}/propose", {"starts_at": new_time}, format="json").json()
    assert moved["status"] == "requested" and moved["starts_at"][:16] == new_time[:16]
    assert Message.objects.filter(conversation=chat, sender=head).count() == 2
    # Only the staff in the conversation can answer.
    assert api(TEACHER_ANITA).post(f"/api/v1/console/communication/meetings/{two.id}/accept").status_code == 403


@pytest.mark.django_db
def test_draft_attachment_and_delete(principal):
    pdf = SimpleUploadedFile("timetable.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    made = principal.post("/api/v1/console/communication/announcements", {"intent": "draft", "title": "T", "body": "B", "scope": "staff", "attachment": pdf}, format="multipart").json()
    assert made["attachment"]["name"] == "timetable.pdf"
    assert principal.delete(f"/api/v1/console/communication/drafts/{made['id']}").status_code == 204
    assert not Announcement.objects.filter(id=made["id"]).exists()


# ------------------------------------------------------------------ documents


@pytest.mark.django_db
def test_documents_folders_upload_permissions_and_audited_downloads(principal):
    from apps.documents.models import Document, DocumentDownload
    from apps.documents.views import visible_documents

    assert api(TEACHER_ANITA).get("/api/v1/console/documents").status_code == 403
    made = principal.post("/api/v1/console/documents/folders", {"name": "Circulars", "locked": False}, format="json")
    assert made.status_code == 201
    folder = made.json()["id"]
    assert principal.post("/api/v1/console/documents/folders", {"name": "circulars"}, format="json").status_code == 400

    link = StudentGuardian.objects.filter(user=user(PARENT_MEERA)).select_related("student__class_group").first()
    group = link.student.class_group
    pdf = SimpleUploadedFile("Trip consent.pdf", b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF", content_type="application/pdf")
    up = principal.post("/api/v1/console/documents", {"file": pdf, "folder_id": folder, "access": "parents", "class_ids": [str(group.id)], "description": "Sign by Friday"}, format="multipart")
    assert up.status_code == 201, up.content
    doc_id = up.json()["id"]
    assert up.json()["access"]["kind"] == "parents" and up.json()["pages"] == 1
    listing = principal.get(f"/api/v1/console/documents?folder={folder}").json()
    assert listing["folder"]["name"] == "Circulars" and [i["id"] for i in listing["items"]] == [doc_id]
    assert principal.get(f"/api/v1/console/documents?folder={folder}&type=xls").json()["total"] == 0
    assert principal.get(f"/api/v1/console/documents?folder={folder}&access=parents").json()["total"] == 1
    # Uploading the same name again is version 2.
    again = SimpleUploadedFile("Trip consent.pdf", b"%PDF-1.4 v2", content_type="application/pdf")
    assert principal.post("/api/v1/console/documents", {"file": again, "folder_id": folder, "access": "parents", "class_ids": [str(group.id)]}, format="multipart").json()["version"] == 2

    doc = Document.objects.get(id=doc_id)
    assert visible_documents(link.student).filter(id=doc_id).exists()
    detail = principal.get(f"/api/v1/console/documents/{doc_id}").json()
    subjects = [r["subject"] for r in detail["permissions"]]
    assert subjects[0] == "principal" and "parents" in subjects and detail["reach"]["unit"] == "families"

    # Families can't be given upload rights (fixed by policy).
    rows = [{"subject": r["subject"], "view": r["view"], "download": r["download"], "upload": r["upload"]} for r in detail["permissions"][1:]]
    bad = [dict(r, upload=True) if r["subject"] == "parents" else r for r in rows]
    assert principal.patch(f"/api/v1/console/documents/{doc_id}/permissions", {"rows": bad}, format="json").status_code == 400
    # View-only for parents: they still see it, but can't download it.
    view_only = [dict(r, download=False) if r["subject"] in ("parents", "students") else r for r in rows]
    saved = principal.patch(f"/api/v1/console/documents/{doc_id}/permissions", {"rows": view_only}, format="json")
    assert saved.status_code == 200 and saved.json()["recent"][0]["action"] == "permissions"
    assert AuditLog.objects.filter(action="documents.permissions", target_id=doc_id).exists()
    assert api(PARENT_MEERA).get(f"/api/v1/documents/{doc_id}/file").status_code == 403
    hidden = [dict(r, view=False, download=False) if r["subject"] in ("parents", "students") else r for r in rows]
    principal.patch(f"/api/v1/console/documents/{doc_id}/permissions", {"rows": hidden}, format="json")
    assert not visible_documents(link.student).filter(id=doc_id).exists()

    # Every console download is logged with the device and audited.
    got = principal.get(f"/api/v1/console/documents/{doc_id}/file", HTTP_USER_AGENT="pytest-browser")
    assert got.status_code == 200
    entry = DocumentDownload.objects.filter(document=doc, action="download").latest("created_at")
    assert entry.device == "pytest-browser" and entry.user == user(PRINCIPAL)
    assert AuditLog.objects.filter(action="documents.download", target_id=doc_id).exists()
    log = principal.get(f"/api/v1/console/documents/{doc_id}/log").json()
    assert log["total"] >= 4 and {"upload", "permissions", "download"} <= {i["action"] for i in log["items"]}

    # Only empty folders can be deleted; locking is audited.
    assert principal.delete(f"/api/v1/console/documents/folders/{folder}").status_code == 400
    assert principal.patch(f"/api/v1/console/documents/folders/{folder}", {"locked": True}, format="json").json()["locked"] is True
    assert AuditLog.objects.filter(action="documents.folder_lock").exists()


@pytest.mark.django_db
def test_document_upload_validation(principal):
    assert principal.post("/api/v1/console/documents", {"access": "staff"}, format="multipart").status_code == 400
    f = SimpleUploadedFile("x.pdf", b"%PDF", content_type="application/pdf")
    bad = principal.post("/api/v1/console/documents", {"file": f, "access": "parents"}, format="multipart")
    assert bad.status_code == 400 and "class_ids" in bad.json()["error"]["fields"]


# ------------------------------------------------------------------ reports


@pytest.mark.django_db
def test_reports_page_charts_and_filters(principal):
    assert api(TEACHER_ANITA).get("/api/v1/console/reports").status_code == 403
    page = principal.get("/api/v1/console/reports").json()
    assert {"attendance", "unit_tests", "fees"} == set(page["charts"])
    assert [r["key"] for r in page["library"]] == ["attendance_register", "class_performance", "fee_collection", "defaulters", "staff_attendance", "transport_utilisation", "admissions_funnel"]
    assert page["can_export"] is True
    grade = ClassGroup.objects.order_by("grade").first().grade
    narrowed = principal.get(f"/api/v1/console/reports?grades={grade}&from=2026-01-01&to=2026-12-31").json()
    assert narrowed["filters"]["grades"] == [grade] and narrowed["filters"]["start"] == "2026-01-01"


@pytest.mark.django_db
@pytest.mark.parametrize("report,fmt,magic", [("attendance_register", "pdf", b"%PDF"), ("fee_collection", "xlsx", b"PK"), ("defaulters", "xlsx", b"PK"), ("admissions_funnel", "pdf", b"%PDF"), ("summary", "xlsx", b"PK")])
def test_generate_reports_real_files_and_audited(principal, report, fmt, magic):
    from apps.reports.models import ReportRun

    made = principal.post("/api/v1/console/reports/generate", {"report": report, "format": fmt}, format="json")
    assert made.status_code == 201, made.content
    run = ReportRun.objects.get(id=made.json()["id"])
    assert run.file.read()[:4].startswith(magic)
    assert AuditLog.objects.filter(action="reports.export", target_id=str(run.id)).exists()
    got = principal.get(made.json()["file"].replace("/console", "/api/v1/console", 1))
    assert got.status_code == 200 and AuditLog.objects.filter(action="reports.download", target_id=str(run.id)).exists()
    # Someone who neither made it nor received it can't download it.
    assert api(TEACHER_ANITA).get(f"/api/v1/console/reports/runs/{run.id}/file").status_code == 404


@pytest.mark.django_db
def test_generate_validation(principal):
    assert principal.post("/api/v1/console/reports/generate", {"report": "nope", "format": "pdf"}, format="json").status_code == 400
    # Defaulters only comes as a spreadsheet.
    assert principal.post("/api/v1/console/reports/generate", {"report": "defaulters", "format": "pdf"}, format="json").status_code == 400


@pytest.mark.django_db
def test_schedules_run_deliver_and_pause(principal):
    from django.core.management import call_command

    from apps.notifications.models import Notification
    from apps.reports.models import ReportDelivery, ScheduledReport

    head = user(PRINCIPAL)
    bad = principal.post("/api/v1/console/reports/schedules", {"name": "x", "report": "defaulters", "format": "pdf", "frequency": "weekly", "recipient_ids": [str(head.id)]}, format="json")
    assert bad.status_code == 400
    teacher = user(TEACHER_ANITA)
    wrong = principal.post("/api/v1/console/reports/schedules", {"name": "x", "report": "defaulters", "format": "xlsx", "frequency": "weekly", "recipient_ids": [str(teacher.id)]}, format="json")
    assert wrong.status_code == 400
    made = principal.post(
        "/api/v1/console/reports/schedules",
        {"name": "Fee defaulters summary", "report": "defaulters", "format": "xlsx", "frequency": "weekly", "weekday": 4, "at": "17:00", "recipient_ids": [str(head.id)]},
        format="json",
    )
    assert made.status_code == 201
    s = ScheduledReport.objects.get(id=made.json()["id"])
    assert s.next_run_at and timezone.localtime(s.next_run_at).weekday() == 4
    # Due now: the command generates and delivers it in-app.
    ScheduledReport.objects.filter(pk=s.pk).update(next_run_at=timezone.now() - timedelta(minutes=1))
    call_command("run_scheduled_reports")
    s.refresh_from_db()
    assert s.last_run_at and s.next_run_at > timezone.now()
    assert ReportDelivery.objects.filter(schedule=s, user=head, channel="in_app").exists()
    assert Notification.objects.filter(user=head, data__type="report").exists()
    log = principal.get("/api/v1/console/reports/deliveries").json()["items"]
    assert log[0]["schedule"] == "Fee defaulters summary"
    # The recipient can download the delivered file.
    assert principal.get(log[0]["file"].replace("/console", "/api/v1/console", 1)).status_code == 200
    # Pause, then run now.
    paused = principal.patch(f"/api/v1/console/reports/schedules/{s.id}", {"enabled": False}, format="json").json()
    assert paused["enabled"] is False and paused["next_run_at"] is None
    assert principal.post(f"/api/v1/console/reports/schedules/{s.id}/run").status_code == 201
    assert principal.delete(f"/api/v1/console/reports/schedules/{s.id}").status_code == 204


@pytest.mark.django_db
def test_custom_report_builder(principal):
    from apps.reports.models import CustomReport, ReportRun

    bad = principal.post("/api/v1/console/reports/custom", {"name": "Houses", "module": "students", "columns": ["name", "shoe_size"]}, format="json")
    assert bad.status_code == 400
    made = principal.post("/api/v1/console/reports/custom", {"name": "Houses", "module": "students", "columns": ["name", "class", "house", "attendance"]}, format="json")
    assert made.status_code == 201
    key = made.json()["key"]
    assert any(c["key"] == key for c in principal.get("/api/v1/console/reports").json()["custom"])
    run = principal.post("/api/v1/console/reports/generate", {"report": key, "format": "xlsx"}, format="json")
    assert run.status_code == 201 and ReportRun.objects.get(id=run.json()["id"]).rows > 0
    assert principal.delete(f"/api/v1/console/reports/custom/{made.json()['id']}").status_code == 204
    assert not CustomReport.objects.exists()
    assert api(TEACHER_ANITA).post("/api/v1/console/reports/custom", {"name": "x", "module": "staff", "columns": ["name"]}, format="json").status_code == 403
