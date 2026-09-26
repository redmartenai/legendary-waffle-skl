import uuid

import pytest

from apps.academics.models import ClassGroup, Student
from apps.notifications.models import Notification

from .conftest import PARENT_MEERA, PRINCIPAL, TEACHER_ANITA, TEACHER_VIKRAM, api, user


@pytest.mark.django_db
def test_teacher_marks_absence_and_parent_is_alerted_once(ghis, in_ghis):
    group = ClassGroup.objects.get(grade="6", section="A")
    diya = Student.objects.get(full_name="Diya Iyer")
    teacher = api(TEACHER_VIKRAM, ghis)

    roster = teacher.get(f"/api/v1/classes/{group.id}/roster").json()
    assert roster["marked"] is False and len(roster["students"]) == 10

    payload = {"entries": [{"student_id": str(diya.id), "status": "absent"}], "client_id": str(uuid.uuid4())}
    first = teacher.post(f"/api/v1/classes/{group.id}/attendance", payload, format="json")
    assert first.status_code == 200 and first.json()["absent"] == 1 and first.json()["present"] == 9

    # Re-submitting (retry or correction with the same status) must not alert the family again.
    teacher.post(f"/api/v1/classes/{group.id}/attendance", payload, format="json")
    alerts = Notification.objects.filter(user=user(PARENT_MEERA), category="attendance", data__student_id=str(diya.id))
    assert alerts.count() == 1
    assert "absent" in alerts.first().title

    after = teacher.get(f"/api/v1/classes/{group.id}/roster").json()
    assert next(s for s in after["students"] if s["id"] == str(diya.id))["status"] == "absent"


@pytest.mark.django_db
def test_teacher_cannot_mark_a_class_they_do_not_teach(ghis, in_ghis):
    group = ClassGroup.objects.get(grade="6", section="A")  # Anita teaches 8B and 10C only
    response = api(TEACHER_ANITA, ghis).post(f"/api/v1/classes/{group.id}/attendance", {"entries": []}, format="json")
    assert response.status_code == 403


@pytest.mark.django_db
def test_parent_cannot_mark_attendance(parent, in_ghis):
    group = ClassGroup.objects.get(grade="8", section="B")
    assert parent.post(f"/api/v1/classes/{group.id}/attendance", {"entries": []}, format="json").status_code == 403


@pytest.mark.django_db
def test_marked_register_locks_after_cutoff(ghis, in_ghis):
    group = ClassGroup.objects.get(grade="6", section="A")
    diya = Student.objects.get(full_name="Diya Iyer")
    teacher = api(TEACHER_VIKRAM, ghis)
    ghis.settings = {**(ghis.settings or {}), "attendance": {"edit_cutoff": "00:00"}}
    ghis.save(update_fields=["settings"])
    first = {"entries": [{"student_id": str(diya.id), "status": "absent"}]}
    assert teacher.post(f"/api/v1/classes/{group.id}/attendance", first, format="json").status_code == 200
    assert teacher.get(f"/api/v1/classes/{group.id}/roster").json()["locked"] is True
    # The same marks again (a retry) are fine; a change needs the principal.
    assert teacher.post(f"/api/v1/classes/{group.id}/attendance", first, format="json").status_code == 200
    change = {"entries": [{"student_id": str(diya.id), "status": "late"}]}
    sent = teacher.post(f"/api/v1/classes/{group.id}/attendance", change, format="json")
    assert sent.status_code == 202 and sent.json()["changes"] == 1
    # Nothing changes until the principal approves.
    roster = teacher.get(f"/api/v1/classes/{group.id}/roster").json()
    assert next(s for s in roster["students"] if s["id"] == str(diya.id))["status"] == "absent"
    principal = api(PRINCIPAL, ghis)
    item = principal.get("/api/v1/approvals", {"kind": "attendance"}).json()["items"][0]
    assert item["details"]["entries"][0]["to"] == "late"
    principal.post(f"/api/v1/approvals/{item['id']}/decide", {"decision": "approve"}, format="json")
    roster = teacher.get(f"/api/v1/classes/{group.id}/roster").json()
    assert next(s for s in roster["students"] if s["id"] == str(diya.id))["status"] == "late"
