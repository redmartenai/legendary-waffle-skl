import uuid

import pytest

from apps.academics.models import ClassGroup, Student
from apps.notifications.models import Notification

from .conftest import PARENT_MEERA, TEACHER_ANITA, TEACHER_VIKRAM, api, user


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
