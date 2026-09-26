"""Console: school settings (roles & permissions, audit log, profile, calendar) and the attendance page."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.academics.models import ClassGroup, Student
from apps.accounts.models import AuditLog, CustomRole, Role, RolePermission
from apps.accounts.permissions import default_cell, has_permission
from apps.approvals.models import ApprovalRequest
from apps.approvals.services import open_request
from apps.attendance.models import AbsenceContact, AttendanceCorrection, AttendanceException, AttendanceSession
from apps.core.utils import school_today
from apps.notifications.models import Notification

from .conftest import PARENT_MEERA, PRINCIPAL, TEACHER_ANITA, TEACHER_VIKRAM, api, user

ADMIN = "+919800000010"  # Kavya Reddy, front office (school admin role)


def _deny(role, module, action, **extra):
    """Store a matrix row for ``role`` with ``action`` switched off."""
    cell = {**default_cell(role, module), action: False, **extra}
    row = RolePermission(role=role, module=module)
    row.set_cell(cell)
    row.save()
    return row


class _Req:
    def __init__(self, phone, roles):
        self.user = user(phone)
        self.roles = frozenset(roles)


# ------------------------------------------------------------------ enforcement


@pytest.mark.django_db
def test_defaults_allow_what_each_role_already_did(in_ghis):
    assert has_permission(_Req(TEACHER_ANITA, {Role.TEACHER}), "attendance", "create")
    assert has_permission(_Req(TEACHER_ANITA, {Role.TEACHER}), "homework", "create")
    assert has_permission(_Req(PARENT_MEERA, {Role.PARENT}), "fees", "download")
    assert has_permission(_Req(PRINCIPAL, {Role.PRINCIPAL}), "fees", "export")
    # Teachers have no fee access at all; parents can't publish; nothing is allowed on a not-applicable action.
    assert not has_permission(_Req(TEACHER_ANITA, {Role.TEACHER}), "fees", "download")
    assert not has_permission(_Req(PARENT_MEERA, {Role.PARENT}), "homework", "publish")
    assert not has_permission(_Req(PRINCIPAL, {Role.PRINCIPAL}), "reports", "create")


@pytest.mark.django_db
def test_unticked_attendance_create_blocks_the_register_endpoint(ghis, in_ghis):
    group = ClassGroup.objects.get(grade="6", section="A")
    diya = Student.objects.get(full_name="Diya Iyer")
    teacher = api(TEACHER_VIKRAM, ghis)
    payload = {"entries": [{"student_id": str(diya.id), "status": "absent"}]}
    _deny(Role.TEACHER, "attendance", "create")
    denied = teacher.post(f"/api/v1/classes/{group.id}/attendance", payload, format="json")
    assert denied.status_code == 403
    # The principal is unaffected (their own role still allows it).
    assert api(PRINCIPAL, ghis).post(f"/api/v1/classes/{group.id}/attendance", payload, format="json").status_code == 200
    RolePermission.objects.filter(role=Role.TEACHER, module="attendance").delete()
    assert teacher.post(f"/api/v1/classes/{group.id}/attendance", payload, format="json").status_code == 200


@pytest.mark.django_db
def test_data_scope_none_blocks_even_with_the_box_ticked(ghis, in_ghis):
    group = ClassGroup.objects.get(grade="6", section="A")
    _deny(Role.TEACHER, "attendance", "delete", data_scope="none")  # view is still ticked
    assert api(TEACHER_VIKRAM, ghis).get(f"/api/v1/classes/{group.id}/roster").status_code == 403


@pytest.mark.django_db
def test_homework_create_permission(ghis, in_ghis):
    group = ClassGroup.objects.get(grade="6", section="A")
    teacher = api(TEACHER_VIKRAM, ghis)
    _deny(Role.TEACHER, "homework", "create")
    # GET on the same endpoint isn't guarded by the POST permission.
    assert teacher.get(f"/api/v1/classes/{group.id}/homework").status_code == 200
    assert teacher.post(f"/api/v1/classes/{group.id}/homework", {"title": "x"}, format="json").status_code == 403


@pytest.mark.django_db
def test_fee_download_permission_and_custom_role_grant(ghis, in_ghis, parent):
    aarav = Student.objects.get(full_name="Aarav Iyer")
    docs = parent.get(f"/api/v1/students/{aarav.id}/documents").json()
    receipt = "/api/v1" + docs["receipts"][0]["download"]
    assert parent.get(receipt).status_code == 200
    _deny(Role.PARENT, "fees", "download")
    assert parent.get(receipt).status_code == 403

    # A custom role adds to what a user's system roles allow.
    request = _Req(TEACHER_ANITA, {Role.TEACHER})
    assert not has_permission(request, "fees", "download")
    role = CustomRole.objects.create(key="custom-bursar", name="Bursar")
    role.members.add(request.user)
    RolePermission.objects.create(role="custom-bursar", module="fees", can_view=True, can_download=True, data_scope="school")
    assert has_permission(_Req(TEACHER_ANITA, {Role.TEACHER}), "fees", "download")


# ------------------------------------------------------------------ roles & permissions page


@pytest.mark.django_db
def test_roles_list_counts_and_role_denial(ghis, in_ghis, principal, teacher, parent):
    body = principal.get("/api/v1/console/settings/roles").json()
    teachers = next(r for r in body["roles"] if r["key"] == "teacher")
    assert teachers["users"] >= 2 and teachers["system"] and body["can_edit"]
    assert next(r for r in body["roles"] if r["key"] == "principal")["editable"] is False
    assert teacher.get("/api/v1/console/settings/roles").status_code == 403
    assert parent.get("/api/v1/console/settings").status_code == 403


@pytest.mark.django_db
def test_save_role_writes_rows_and_audit(ghis, in_ghis, principal):
    matrix = principal.get("/api/v1/console/settings/roles/teacher").json()
    transport = next(m for m in matrix["modules"] if m["module"] == "transport")
    assert transport["cells"]["view"]["allowed"] is False and transport["cells"]["create"]["locked"] is True
    assert transport["cells"]["approve"]["na"] is True

    saved = principal.put(
        "/api/v1/console/settings/roles/teacher",
        {"modules": {"transport": {"view": True}, "reports": {"export": True, "data_scope": "school"}}},
        format="json",
    )
    assert saved.status_code == 200 and saved.json()["saved"] == 3
    assert RolePermission.objects.get(role="teacher", module="transport").can_view is True
    summaries = set(AuditLog.objects.filter(action="role.permission").values_list("summary", flat=True))
    assert {"Teacher · Transport → View on", "Teacher · Reports → Export on", "Teacher · Reports → Entire school"} <= summaries
    assert has_permission(_Req(TEACHER_ANITA, {Role.TEACHER}), "transport", "view")
    recent = principal.get("/api/v1/console/settings/roles").json()["recent"]
    assert recent[0]["summary"].startswith("Teacher ·")


@pytest.mark.django_db
def test_locked_and_not_applicable_cells_cannot_change(ghis, in_ghis, principal):
    locked = principal.put("/api/v1/console/settings/roles/teacher", {"modules": {"fees": {"view": True}}}, format="json")
    assert locked.status_code == 400 and "locked" in str(locked.json()["error"]["fields"]["fees"])
    na = principal.put("/api/v1/console/settings/roles/teacher", {"modules": {"reports": {"publish": True}}}, format="json")
    assert na.status_code == 400
    boss = principal.put("/api/v1/console/settings/roles/principal", {"modules": {"fees": {"export": False}}}, format="json")
    assert boss.status_code == 400
    assert principal.put("/api/v1/console/settings/roles/teacher", {"modules": {"students": {"data_scope": "everyone"}}}, format="json").status_code == 400
    assert not RolePermission.objects.exists() and not AuditLog.objects.filter(module="roles").exists()


@pytest.mark.django_db
def test_only_the_principal_changes_roles(ghis, in_ghis):
    office = api(ADMIN, ghis)
    assert office.get("/api/v1/console/settings/roles/teacher").status_code == 200
    assert office.put("/api/v1/console/settings/roles/teacher", {"modules": {"transport": {"view": True}}}, format="json").status_code == 403
    assert office.post("/api/v1/console/settings/roles", {"name": "Librarian"}, format="json").status_code == 403


@pytest.mark.django_db
def test_custom_role_lifecycle(ghis, in_ghis, principal):
    created = principal.post("/api/v1/console/settings/roles", {"name": "Librarian", "based_on": "teacher"}, format="json")
    assert created.status_code == 201
    key = created.json()["key"]
    # It starts as a copy of the teacher's matrix.
    homework = next(m for m in created.json()["modules"] if m["module"] == "homework")
    assert homework["cells"]["publish"]["allowed"] is True and homework["cells"]["publish"]["locked"] is False
    assert principal.post("/api/v1/console/settings/roles", {"name": "librarian"}, format="json").status_code == 400
    assert principal.post("/api/v1/console/settings/roles", {"name": ""}, format="json").status_code == 400

    anita = user(TEACHER_ANITA)
    added = principal.post(f"/api/v1/console/settings/roles/{key}/members", {"user_id": str(anita.id)}, format="json")
    assert added.status_code == 201 and added.json()["users"] == 1
    assert principal.post(f"/api/v1/console/settings/roles/{key}/members", {"user_id": str(user(PARENT_MEERA).id)}, format="json").status_code == 400

    assert principal.delete("/api/v1/console/settings/roles/teacher").status_code == 400
    assert principal.delete(f"/api/v1/console/settings/roles/{key}").status_code == 204
    assert not RolePermission.objects.filter(role=key).exists()
    actions = list(AuditLog.objects.filter(module="roles").values_list("action", flat=True))
    assert {"role.create", "role.assign", "role.delete"} <= set(actions)


@pytest.mark.django_db
def test_audit_log_filters(ghis, in_ghis, principal, teacher):
    principal.put("/api/v1/console/settings/roles/teacher", {"modules": {"transport": {"view": True}}}, format="json")
    principal.patch("/api/v1/console/settings/profile", {"campus": "North Campus"}, format="json")
    everything = principal.get("/api/v1/console/settings/audit").json()
    assert everything["total"] >= 2 and {"roles", "settings"} <= set(everything["modules"])
    roles = principal.get("/api/v1/console/settings/audit?module=roles").json()
    assert roles["total"] == 1 and roles["items"][0]["actor"] == user(PRINCIPAL).full_name
    assert principal.get("/api/v1/console/settings/audit?q=Transport").json()["total"] == 1
    assert principal.get("/api/v1/console/settings/audit?from=2001-01-01&to=2001-01-02").json()["total"] == 0
    assert principal.get("/api/v1/console/settings/audit?from=yesterday").status_code == 400
    assert teacher.get("/api/v1/console/settings/audit").status_code == 403


# ------------------------------------------------------------------ other settings sections


@pytest.mark.django_db
def test_settings_overview_and_profile(ghis, in_ghis, principal):
    body = principal.get("/api/v1/console/settings").json()
    assert body["profile"]["name"] == ghis.name and body["roles"] >= 9
    assert {i["key"]: i["status"] for i in body["integrations"]}["sms"] == "off"
    bad = principal.patch("/api/v1/console/settings/profile", {"name": "", "email": "nope", "contacts": {"office": "12"}}, format="json")
    assert bad.status_code == 400 and {"name", "email", "contacts.office"} <= set(bad.json()["error"]["fields"])
    ok = principal.patch("/api/v1/console/settings/profile", {"campus": "Hill Campus", "contacts": {"office": "98450 12345"}}, format="json")
    assert ok.status_code == 200 and ok.json()["campus"] == "Hill Campus" and ok.json()["contacts"]["office"] == "+919845012345"
    assert AuditLog.objects.filter(action="settings.profile").exists()


@pytest.mark.django_db
def test_calendar_validation(ghis, in_ghis, principal):
    overlap = [{"name": "Term 1", "starts_on": "2026-04-01", "ends_on": "2026-10-10"}, {"name": "Term 2", "starts_on": "2026-10-01", "ends_on": "2027-03-31"}]
    assert principal.put("/api/v1/console/settings/calendar", {"terms": overlap}, format="json").status_code == 400
    sunday = principal.put("/api/v1/console/settings/calendar", {"terms": overlap[:1], "holidays": [{"date": "2026-10-04", "name": "X"}]}, format="json")
    assert sunday.status_code == 400
    ok = principal.put(
        "/api/v1/console/settings/calendar",
        {"terms": overlap[:1], "holidays": [{"date": "2026-10-02", "name": "Gandhi Jayanti"}]},
        format="json",
    )
    assert ok.status_code == 200 and ok.json()["holidays"] == [{"date": "2026-10-02", "name": "Gandhi Jayanti"}]


@pytest.mark.django_db
def test_notification_settings(ghis, in_ghis, principal):
    assert principal.patch("/api/v1/console/settings/notifications", {"quiet_hours": ["25:00", "07:00"]}, format="json").status_code == 400
    assert principal.patch("/api/v1/console/settings/notifications", {"absence_template": "Your child is absent"}, format="json").status_code == 400
    ok = principal.patch("/api/v1/console/settings/notifications", {"quiet_hours": ["22:00", "06:30"], "absence_template": "{child} is absent today."}, format="json")
    assert ok.status_code == 200 and ok.json()["quiet_hours"] == ["22:00", "06:30"]


# ------------------------------------------------------------------ attendance page


def _mark_today(ghis, grade, section, absent=(), late=()):
    group = ClassGroup.objects.get(grade=grade, section=section)
    session, _ = AttendanceSession.objects.get_or_create(class_group=group, date=school_today(ghis), defaults={"marked_at": timezone.now()})
    for s in absent:
        AttendanceException.objects.update_or_create(session=session, student=s, defaults={"status": "absent"})
    for s in late:
        AttendanceException.objects.update_or_create(session=session, student=s, defaults={"status": "late"})
    return session


@pytest.mark.django_db
def test_attendance_page_and_role_denial(ghis, in_ghis, principal, teacher):
    diya = Student.objects.get(full_name="Diya Iyer")
    _mark_today(ghis, "6", "A", absent=[diya])
    body = principal.get("/api/v1/console/attendance").json()
    assert body["is_today"] and body["roll"]["absent"] >= 1 and body["roll"]["unexplained"] >= 1
    assert len(body["heat"]["days"]) == 10 and any(g["sections"] for g in body["heat"]["groups"])
    assert body["alerts"]["pending"] == body["roll"]["unexplained"]
    assert teacher.get("/api/v1/console/attendance").status_code == 403
    assert principal.get("/api/v1/console/attendance?date=2999-01-01").status_code == 400
    assert principal.get("/api/v1/console/attendance?view=staff").status_code == 200
    missing = principal.get("/api/v1/console/attendance/absentees?sections=6-A").json()["items"]
    assert [m["name"] for m in missing] == ["Diya Iyer"]


@pytest.mark.django_db
def test_absence_alerts_are_sent_once_a_day(ghis, in_ghis, principal):
    diya = Student.objects.get(full_name="Diya Iyer")
    _mark_today(ghis, "6", "A", absent=[diya])
    sent = principal.post("/api/v1/console/attendance/alerts", {}, format="json")
    assert sent.status_code == 200 and sent.json()["students"] >= 1
    alerts = Notification.objects.filter(user=user(PARENT_MEERA), data__type="absence_alert", data__student_id=str(diya.id))
    assert alerts.count() == 1
    assert AbsenceContact.objects.filter(student=diya, channel="alert").count() == 1
    assert AuditLog.objects.filter(action="attendance.alert").exists()
    again = principal.post("/api/v1/console/attendance/alerts", {}, format="json")
    assert again.status_code == 400 and alerts.count() == 1
    assert sent.json()["payload"]["alerts"]["pending"] == 0


@pytest.mark.django_db
def test_log_a_call(ghis, in_ghis, principal):
    diya = Student.objects.get(full_name="Diya Iyer")
    assert principal.post("/api/v1/console/attendance/contacts", {"student_id": str(diya.id), "outcome": "maybe"}, format="json").status_code == 400
    ok = principal.post("/api/v1/console/attendance/contacts", {"student_id": str(diya.id), "outcome": "reached", "note": "Fever"}, format="json")
    assert ok.status_code == 201 and AbsenceContact.objects.get(student=diya).outcome == "reached"


def _slip(ghis, before, after):
    group = ClassGroup.objects.get(grade="6", section="A")
    diya = Student.objects.get(full_name="Diya Iyer")
    session, _ = AttendanceSession.objects.get_or_create(class_group=group, date=school_today(ghis) - timedelta(days=3), defaults={"marked_at": timezone.now()})
    AttendanceException.objects.filter(session=session, student=diya).delete()
    if before != "present":
        AttendanceException.objects.create(session=session, student=diya, status=before)
    corr = AttendanceCorrection.objects.create(session=session, entries=[{"student_id": str(diya.id), "from": before, "to": after}], reason="Came late", requested_by=user(TEACHER_VIKRAM))
    req = open_request(kind="attendance", target=corr, requested_by=user(TEACHER_VIKRAM), summary="6A", notify_principal=False)
    return req, session, diya, corr


@pytest.mark.django_db
def test_correction_slip_approve_and_undo(ghis, in_ghis, principal, teacher):
    req, session, diya, corr = _slip(ghis, "absent", "present")
    assert corr.code.startswith("AC-")
    slips = principal.get("/api/v1/console/attendance").json()["corrections"]
    assert any(s["id"] == str(req.id) and s["student"] == "Diya Iyer" for s in slips)
    assert teacher.post(f"/api/v1/console/attendance/corrections/{req.id}/decide", {"decision": "approve"}, format="json").status_code == 403
    assert principal.post(f"/api/v1/console/attendance/corrections/{req.id}/decide", {"decision": "maybe"}, format="json").status_code == 400
    done = principal.post(f"/api/v1/console/attendance/corrections/{req.id}/decide", {"decision": "approve"}, format="json")
    assert done.status_code == 200
    assert not AttendanceException.objects.filter(session=session, student=diya).exists()
    assert ApprovalRequest.objects.get(id=req.id).status == "approved"
    assert AuditLog.objects.filter(action="approval.approve", module="attendance").exists()
    slip = next(s for s in done.json()["corrections"] if s["id"] == str(req.id))
    assert slip["undo_until"]
    assert principal.post(f"/api/v1/console/attendance/corrections/{req.id}/decide", {"decision": "approve"}, format="json").status_code == 400
    assert principal.post(f"/api/v1/console/attendance/corrections/{req.id}/undo", {}, format="json").status_code == 200
    assert AttendanceException.objects.get(session=session, student=diya).status == "absent"


@pytest.mark.django_db
def test_correction_slip_reject_keeps_register(ghis, in_ghis, principal):
    req, session, diya, _corr = _slip(ghis, "present", "absent")
    assert principal.post(f"/api/v1/console/attendance/corrections/{req.id}/decide", {"decision": "decline"}, format="json").status_code == 200
    assert not AttendanceException.objects.filter(session=session, student=diya).exists()
    assert ApprovalRequest.objects.get(id=req.id).status == "declined"


@pytest.mark.django_db
def test_handoff_needs_chronic_absentees(ghis, in_ghis, principal):
    assert principal.post("/api/v1/console/attendance/handoff", {}, format="json").status_code == 400
