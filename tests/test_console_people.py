"""Console people pages: Students (directory, export, bulk message, add), Student profile, Admissions, Staff."""

from datetime import date, datetime, time, timedelta

import pytest
from django.utils import timezone

from apps.academics.models import ClassGroup, Remark, Student, StudentGuardian
from apps.accounts.models import AuditLog, Membership, Role, User
from apps.admissions.models import AdmissionCycle, Application, ApplicationEvent, SeatPlan
from apps.approvals.models import ApprovalRequest
from apps.approvals.services import open_request
from apps.messaging.models import Conversation, Message
from apps.staff.models import StaffAttendance, StaffLeave, Vacancy

from .conftest import PARENT_MEERA, PRINCIPAL, TEACHER_ANITA, TEACHER_VIKRAM, api, user


@pytest.fixture
def principal(in_ghis):
    return api(PRINCIPAL)


def _aarav():
    return Student.objects.get(full_name="Aarav Mehta") if Student.objects.filter(full_name="Aarav Mehta").exists() else StudentGuardian.objects.filter(user=user(PARENT_MEERA)).first().student


# ---------------------------------------------------------------------------------------------- students


@pytest.mark.django_db
def test_directory_pages_filters_and_counts(principal):
    res = principal.get("/api/v1/console/students?page_size=12")
    assert res.status_code == 200
    body = res.json()
    active = Student.objects.filter(is_active=True).count()
    assert body["counts"]["all"] == active == body["total"]
    assert len(body["items"]) == min(12, active)
    names = [r["name"] for r in body["items"]]
    assert names == sorted(names)
    row = body["items"][0]
    assert {"attendance", "ut2", "fee", "transport", "parent", "class", "roll_no", "admission_no"} <= set(row)
    # A class filter (as global search sends it) keeps to that class.
    group = ClassGroup.objects.first()
    only = principal.get(f"/api/v1/console/students?class={group.id}&page_size=50").json()
    assert only["total"] == Student.objects.filter(class_group=group, is_active=True).count()
    assert all(r["class"]["id"] == str(group.id) for r in only["items"])
    # Search by a parent's phone digits finds their children.
    kids = principal.get("/api/v1/console/students?q=" + PARENT_MEERA[3:]).json()
    assert {r["name"] for r in kids["items"]} == {s.full_name for s in Student.objects.filter(guardian_links__user=user(PARENT_MEERA), is_active=True)}
    assert principal.get("/api/v1/console/students?page_size=7").status_code == 400


@pytest.mark.django_db
def test_directory_is_for_management_only(in_ghis):
    assert api(TEACHER_ANITA).get("/api/v1/console/students").status_code == 403
    assert api(PARENT_MEERA).get("/api/v1/console/students").status_code == 403
    assert api(PARENT_MEERA).get("/api/v1/console/students/export").status_code == 403


@pytest.mark.django_db
def test_export_is_audited_csv(principal):
    ids = [str(s.id) for s in Student.objects.filter(is_active=True)[:2]]
    res = principal.get("/api/v1/console/students/export?ids=" + ",".join(ids))
    assert res.status_code == 200 and res["Content-Type"].startswith("text/csv")
    lines = res.content.decode().strip().splitlines()
    assert len(lines) == 3 and lines[0].startswith("Admission no")
    log = AuditLog.objects.filter(action="students.export").latest("created_at")
    assert log.detail["count"] == 2 and log.actor == user(PRINCIPAL)


@pytest.mark.django_db
def test_bulk_message_reaches_each_family(principal):
    students = list(Student.objects.filter(guardian_links__isnull=False, is_active=True).distinct()[:2])
    res = principal.post("/api/v1/console/students/message", {"student_ids": [str(s.id) for s in students], "body": "Fee reminder"}, format="json")
    assert res.status_code == 200 and res.json()["sent"] == 2
    for s in students:
        assert Message.objects.filter(conversation__student=s, sender=user(PRINCIPAL), body="Fee reminder").exists()
    assert principal.post("/api/v1/console/students/message", {"student_ids": [str(students[0].id)], "body": " "}, format="json").status_code == 400
    assert principal.post("/api/v1/console/students/message", {"student_ids": [], "body": "x"}, format="json").status_code == 400


@pytest.mark.django_db
def test_add_student_with_guardian(principal):
    group = ClassGroup.objects.first()
    res = principal.post(
        "/api/v1/console/students",
        {"full_name": "Tara  Bose", "class_id": str(group.id), "date_of_birth": "2016-05-02", "gender": "female", "guardian_name": "Arindam Bose", "guardian_phone": "98450 11122", "relationship": "father"},
        format="json",
    )
    assert res.status_code == 201, res.content
    student = Student.objects.get(id=res.json()["id"])
    assert student.full_name == "Tara Bose" and student.class_group == group
    link = StudentGuardian.objects.get(student=student)
    assert link.user.phone == "+919845011122" and link.relationship == "father" and link.is_primary
    assert Membership.objects.filter(user=link.user, role=Role.PARENT).exists()
    bad = principal.post("/api/v1/console/students", {"full_name": "", "class_id": "nope", "guardian_phone": "12"}, format="json")
    assert bad.status_code == 400
    assert {"full_name", "class_id", "guardian_name", "guardian_phone"} <= set(bad.json()["error"]["fields"])


@pytest.mark.django_db
def test_profile_has_every_tab(principal):
    student = StudentGuardian.objects.filter(user=user(PARENT_MEERA)).first().student
    res = principal.get(f"/api/v1/console/students/{student.id}")
    assert res.status_code == 200
    body = res.json()
    for key in ("attendance", "academics", "remarks", "fees", "homework", "interactions", "documents", "guardians", "siblings", "today"):
        assert key in body
    assert body["guardians"][0]["phone_masked"].endswith(PARENT_MEERA[-4:])
    assert len(body["attendance"]["calendar"]) >= 28
    assert principal.get(f"/api/v1/console/students/{student.id}?month=2026-13").status_code == 400
    assert principal.get("/api/v1/console/students/00000000-0000-0000-0000-000000000000").status_code == 404
    assert api(TEACHER_VIKRAM).get(f"/api/v1/console/students/{student.id}").status_code == 403


@pytest.mark.django_db
def test_message_parent_remark_and_fee_reminder(principal):
    student = StudentGuardian.objects.filter(user=user(PARENT_MEERA)).first().student
    res = principal.post(f"/api/v1/console/students/{student.id}/message", {"body": "Please call me"}, format="json")
    assert res.status_code == 201
    conversation = Conversation.objects.get(id=res.json()["conversation_id"])
    assert conversation.student == student and conversation.messages.filter(body="Please call me").exists()
    # The same conversation is reused.
    again = principal.post(f"/api/v1/console/students/{student.id}/message", {}, format="json").json()
    assert again["conversation_id"] == str(conversation.id)

    made = principal.post(f"/api/v1/console/students/{student.id}/remarks", {"body": "Well done at the science fair", "tone": "positive"}, format="json")
    assert made.status_code == 201 and Remark.objects.get(id=made.json()["id"]).author == user(PRINCIPAL)
    assert principal.post(f"/api/v1/console/students/{student.id}/remarks", {"body": "ok", "tone": "angry"}, format="json").status_code == 400

    from apps.fees.models import FeeInvoice

    FeeInvoice.objects.create(student=student, title="Term 2 tuition", amount=1000, due_date=date.today() + timedelta(days=5))
    reminded = principal.post(f"/api/v1/console/students/{student.id}/fee-reminder")
    assert reminded.status_code == 200
    assert AuditLog.objects.filter(action="fees.remind", target_id=str(student.id)).exists()


@pytest.mark.django_db
def test_report_card_print_is_audited(principal):
    from apps.results.models import Exam

    exam = Exam.objects.filter(is_published=True, marks__isnull=False).first()
    if exam is None:
        pytest.skip("demo school has no published results")
    student = exam.marks.first().student
    res = principal.get(f"/api/v1/console/students/{student.id}/report-card.pdf?exam={exam.id}")
    assert res.status_code == 200 and res["Content-Type"] == "application/pdf"
    assert AuditLog.objects.filter(action="students.report_card", target_id=str(student.id)).exists()


# ---------------------------------------------------------------------------------------------- admissions


def _cycle():
    today = timezone.localdate()
    cycle = AdmissionCycle.objects.create(
        academic_year="2027–28", enquiries_open_on=today - timedelta(days=40), applications_close_on=today + timedelta(days=60),
        session_starts_on=today + timedelta(days=180), offer_rounds=[{"name": "Offer round 2", "on": (today + timedelta(days=20)).isoformat()}],
    )
    SeatPlan.objects.create(cycle=cycle, label="Grade 1", grades=["1"], seats=10, order=0)
    return cycle


def _app(stage="enquiry", **extra):
    n = Application.objects.count() + 1
    return Application.objects.create(
        application_no=f"APP-27-{n:04d}", child_name=f"Child {n}", grade=extra.pop("grade", "1"), academic_year="2027–28",
        guardian_name="Neha Kapoor", guardian_phone=f"+9199887766{n:02d}", stage=stage, stage_changed_at=timezone.now(), **extra,
    )


@pytest.mark.django_db
def test_admissions_page_funnel_and_board(principal):
    _cycle()
    _app("enquiry", source="website")
    _app("enquiry", source="website", closed=True, closed_reason="Moved city")
    _app("assessment", source="walk_in", assessment_score=80)
    _app("admitted", source="referral", status="offered")
    body = principal.get("/api/v1/console/admissions").json()
    f = body["funnel"]
    assert (f["enquiries"], f["still_open"], f["applications"], f["assessed"], f["offers"], f["admitted"]) == (4, 1, 2, 2, 1, 1)
    columns = {c["stage"]: c["count"] for c in body["columns"]}
    assert columns == {"enquiry": 1, "application": 0, "assessment": 1, "documents": 0, "offer": 0, "admitted": 1}
    assert body["seats"]["rows"][0] == {**body["seats"]["rows"][0], "seats": 10, "admitted": 1, "open": 9}
    assert {s["source"]: s["count"] for s in body["sources"]}["website"] == 2
    filtered = principal.get("/api/v1/console/admissions?source=walk_in").json()
    assert sum(c["count"] for c in filtered["columns"]) == 1
    assert api(TEACHER_ANITA).get("/api/v1/console/admissions").status_code == 403


@pytest.mark.django_db
def test_new_enquiry_and_moves(principal):
    _cycle()
    made = principal.post("/api/v1/console/admissions", {"child_name": "Myra Bhatt", "grade": "Nursery", "source": "website", "guardian_name": "Karan Bhatt", "guardian_phone": "9845022233"}, format="json")
    assert made.status_code == 201, made.content
    app_id = made.json()["id"]
    assert principal.post("/api/v1/console/admissions", {"child_name": "", "grade": "13"}, format="json").status_code == 400
    moved = principal.post(f"/api/v1/console/admissions/{app_id}/move", {"to_stage": "application"}, format="json")
    assert moved.status_code == 200 and moved.json()["stage"] == "application"
    # The form fee comes before the assessment; you can't skip stages or jump to an offer.
    assert principal.post(f"/api/v1/console/admissions/{app_id}/move", {"to_stage": "assessment"}, format="json").status_code == 400
    assert principal.post(f"/api/v1/console/admissions/{app_id}/move", {"to_stage": "offer"}, format="json").status_code == 400
    principal.patch(f"/api/v1/console/admissions/{app_id}", {"form_fee_paid": True}, format="json")
    assert principal.post(f"/api/v1/console/admissions/{app_id}/move", {"to_stage": "assessment"}, format="json").status_code == 200
    assert principal.post(f"/api/v1/console/admissions/{app_id}/move", {"to_stage": "documents"}, format="json").status_code == 400
    assert principal.patch(f"/api/v1/console/admissions/{app_id}", {"assessment_score": 140}, format="json").status_code == 400
    principal.patch(f"/api/v1/console/admissions/{app_id}", {"assessment_score": 88}, format="json")
    assert principal.post(f"/api/v1/console/admissions/{app_id}/move", {"to_stage": "documents"}, format="json").status_code == 200
    actions = list(ApplicationEvent.objects.filter(application_id=app_id).order_by("created_at").values_list("action", flat=True))
    assert actions[0] == "created" and actions.count("moved") == 3 and "scored" in actions


@pytest.mark.django_db
def test_verify_approve_and_admit_creates_the_student(principal):
    _cycle()
    grade = ClassGroup.objects.first().grade
    app = _app("documents", assessment_score=90, form_fee_paid=True, documents_pending="TC", grade=grade)
    verified = principal.post(f"/api/v1/console/admissions/{app.id}/verify").json()
    assert verified["tag"]["kind"] == "verified" and verified["approval_id"]
    req = ApprovalRequest.objects.get(id=verified["approval_id"])
    assert req.kind == "admission" and req.status == "pending"
    decided = principal.post(f"/api/v1/console/admissions/{app.id}/decide", {"decision": "approve"}, format="json")
    assert decided.status_code == 200 and decided.json()["stage"] == "offer"
    app.refresh_from_db()
    assert app.status == "offered" and app.offer_reply_by
    assert AuditLog.objects.filter(action="approval.approve", module="admissions").exists()
    # Undoing through the approvals engine puts the card back in Documents.
    principal.post(f"/api/v1/approvals/{req.id}/undo")
    app.refresh_from_db()
    assert app.stage == "documents" and app.status == "applied"
    principal.post(f"/api/v1/approvals/{req.id}/decide", {"decision": "approve"}, format="json")
    app.refresh_from_db()
    assert app.stage == "offer"

    assert principal.post(f"/api/v1/console/admissions/{app.id}/admit", {}, format="json").status_code == 400
    admitted = principal.post(f"/api/v1/console/admissions/{app.id}/admit", {"receipt_no": "SPS-R-25999"}, format="json")
    assert admitted.status_code == 201
    app.refresh_from_db()
    assert app.stage == "admitted" and app.student is not None
    student = app.student
    assert student.full_name == app.child_name and not student.is_active and student.class_group.grade == grade
    assert StudentGuardian.objects.filter(student=student, user__phone=app.guardian_phone).exists()


@pytest.mark.django_db
def test_decline_closes_and_close_needs_a_reason(principal):
    _cycle()
    app = _app("documents", assessment_score=70, documents_verified=True)
    open_request(kind="admission", target=app, requested_by=user(PRINCIPAL), summary="x", notify_principal=False)
    assert principal.post(f"/api/v1/console/admissions/{app.id}/decide", {"decision": "maybe"}, format="json").status_code == 400
    principal.post(f"/api/v1/console/admissions/{app.id}/decide", {"decision": "decline", "note": "No seats"}, format="json")
    app.refresh_from_db()
    assert app.closed and app.status == "declined"
    other = _app("enquiry")
    assert principal.post(f"/api/v1/console/admissions/{other.id}/close", {}, format="json").status_code == 400
    assert principal.post(f"/api/v1/console/admissions/{other.id}/close", {"reason": "No response"}, format="json").json()["closed"] is True
    assert principal.post(f"/api/v1/console/admissions/{other.id}/close", {"reopen": True}, format="json").json()["closed"] is False


# ---------------------------------------------------------------------------------------------- staff


def _checkins(today):
    for m in Membership.objects.filter(role=Role.TEACHER, is_active=True):
        for back in range(3):
            day = today - timedelta(days=back)
            StaffAttendance.objects.create(user=m.user, date=day, status="present", check_in=time(7, 50), source="biometric")


@pytest.mark.django_db
def test_staff_page_kpis_and_table(principal):
    today = timezone.localdate()
    _checkins(today)
    teacher = user(TEACHER_ANITA)
    StaffAttendance.objects.filter(user=teacher, date=today).update(status="leave", check_in=None)
    StaffLeave.objects.create(user=teacher, kind="sick", from_date=today, to_date=today, days=1, reason="Fever", status="approved")
    Vacancy.objects.create(title="Physics (PGT)", applicants=12, opened_on=today)
    body = principal.get("/api/v1/console/staff").json()
    teachers = Membership.objects.filter(role=Role.TEACHER, is_active=True).values("user").distinct().count()
    assert body["counts"]["teaching"] == teachers
    assert body["kpis"]["present"]["count"] == teachers - 1
    assert body["kpis"]["leave"]["count"] == 1 and body["kpis"]["leave"]["people"][0]["name"] == teacher.full_name
    assert body["kpis"]["vacancies"] == {**body["kpis"]["vacancies"], "positions": 1, "applicants": 12}
    row = next(r for r in body["items"] if r["id"] == str(teacher.id))
    assert row["today"]["state"] == "leave" and row["today"]["leave_kind"] == "medical"
    # ?person= jumps to (and highlights) that person.
    jump = principal.get(f"/api/v1/console/staff?person={teacher.id}").json()
    assert jump["highlight"] == str(teacher.id) and any(r["id"] == str(teacher.id) for r in jump["items"])
    support = principal.get("/api/v1/console/staff?tab=support").json()
    assert support["tab"] == "support" and all(r["role"] != "teacher" for r in support["items"])
    assert principal.get("/api/v1/console/staff?tab=robots").status_code == 400
    assert api(TEACHER_ANITA).get("/api/v1/console/staff").status_code == 403


@pytest.mark.django_db
def test_staff_leave_decided_through_approvals(principal):
    teacher = user(TEACHER_ANITA)
    day = timezone.localdate() + timedelta(days=3)
    leave = StaffLeave.objects.create(user=teacher, kind="casual", from_date=day, to_date=day, days=1, reason="Wedding")
    req = open_request(kind="leave", target=leave, requested_by=teacher, summary="Casual", due_on=day, notify_principal=False)
    listed = principal.get("/api/v1/console/staff").json()["leave_requests"]
    assert any(r["id"] == str(req.id) for r in listed)
    assert principal.post(f"/api/v1/console/staff/leave/{req.id}/decide", {"decision": "decline"}, format="json").status_code == 400
    res = principal.post(f"/api/v1/console/staff/leave/{req.id}/decide", {"decision": "approve"}, format="json")
    assert res.status_code == 200 and res.json()["status"] == "approved"
    leave.refresh_from_db()
    assert leave.status == "approved"
    assert AuditLog.objects.filter(action="approval.approve", module="staff").exists()


@pytest.mark.django_db
def test_add_staff_and_export(principal):
    res = principal.post("/api/v1/console/staff", {"full_name": "Asha Rao", "phone": "9845099911", "role": "teacher", "title": "Physics"}, format="json")
    assert res.status_code == 201, res.content
    new = User.objects.get(phone="+919845099911")
    assert Membership.objects.filter(user=new, role=Role.TEACHER).exists()
    assert principal.post("/api/v1/console/staff", {"full_name": "Asha Rao", "phone": "9845099911", "role": "teacher"}, format="json").status_code == 400
    assert principal.post("/api/v1/console/staff", {"full_name": "X", "phone": "1", "role": "wizard"}, format="json").status_code == 400
    csv = principal.get("/api/v1/console/staff/export?tab=teaching")
    assert csv.status_code == 200 and "Asha Rao" in csv.content.decode()
    assert AuditLog.objects.filter(action="staff.export").exists() and AuditLog.objects.filter(action="staff.create").exists()


_ = (datetime, SeatPlan)
