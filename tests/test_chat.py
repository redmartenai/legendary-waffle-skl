import uuid

import pytest

from apps.academics.models import Student
from apps.notifications.models import Notification

from .conftest import PARENT_MEERA, TEACHER_ANITA, api, user


@pytest.mark.django_db
def test_parent_contacts_are_limited_to_their_childrens_teachers(parent):
    contacts = parent.get("/api/v1/chat/contacts").json()["contacts"]
    teacher_names = {c["name"] for c in contacts if c["kind"] == "user"}
    assert "Anita Rao" in teacher_names and "Vikram Das" in teacher_names
    assert "Priya Nair" in teacher_names  # subject teacher (policy allows)
    departments = {(c["department"], c["student"]["first_name"]) for c in contacts if c["kind"] == "department"}
    assert ("transport", "Aarav") in departments and ("accounts", "Diya") in departments
    # No phone numbers anywhere in the contact list.
    assert "+91" not in str(contacts)


@pytest.mark.django_db
def test_parent_teacher_conversation_round_trip(ghis, in_ghis, parent, teacher):
    aarav = Student.objects.get(full_name="Aarav Iyer")
    anita = user(TEACHER_ANITA)

    listing = parent.get("/api/v1/chat/conversations").json()
    existing = next(c for c in listing["conversations"] if c["title"] == "Anita Rao")
    assert existing["student"]["first_name"] == "Aarav"
    assert existing["unread"] == 1  # Anita's latest message
    assert existing["office_hours"]["text"].startswith("Usually replies Mon–Sat")

    started = parent.post(
        "/api/v1/chat/conversations",
        {"kind": "user", "user_id": str(anita.id), "student_id": str(aarav.id)},
        format="json",
    )
    assert started.status_code == 201 and started.json()["id"] == existing["id"]  # reopens, no duplicate

    client_id = str(uuid.uuid4())
    sent = parent.post(f"/api/v1/chat/conversations/{existing['id']}/messages", {"body": "Will Aarav need a calculator on Monday?", "client_id": client_id}, format="json")
    assert sent.status_code == 201
    retry = parent.post(f"/api/v1/chat/conversations/{existing['id']}/messages", {"body": "Will Aarav need a calculator on Monday?", "client_id": client_id}, format="json")
    assert retry.status_code == 200 and retry.json()["id"] == sent.json()["id"]

    teacher_alerts = Notification.objects.filter(user=anita, category="chat", data__conversation_id=existing["id"])
    assert teacher_alerts.count() == 1
    assert "about Aarav" in teacher_alerts.first().title

    messages = teacher.get(f"/api/v1/chat/conversations/{existing['id']}/messages").json()["messages"]
    assert messages[-1]["body"] == "Will Aarav need a calculator on Monday?" and messages[-1]["mine"] is False

    assert teacher.post(f"/api/v1/chat/conversations/{existing['id']}/read").status_code == 204
    teacher_list = teacher.get("/api/v1/chat/conversations").json()
    assert next(c for c in teacher_list["conversations"] if c["id"] == existing["id"])["unread"] == 0


@pytest.mark.django_db
def test_parent_cannot_start_chat_with_unrelated_teacher_or_about_other_child(ghis, in_ghis, parent):
    kabir = Student.objects.get(full_name="Kabir Sharma")
    farah = user("+919800000004")  # teaches 10C and 6A science; Meera's children include 6A Diya
    denied = parent.post(
        "/api/v1/chat/conversations",
        {"kind": "user", "user_id": str(farah.id), "student_id": str(kabir.id)},
        format="json",
    )
    assert denied.status_code == 403
    other_parent = user("+919900000100")
    denied_parent = parent.post(
        "/api/v1/chat/conversations",
        {"kind": "user", "user_id": str(other_parent.id), "student_id": str(Student.objects.get(full_name='Aarav Iyer').id)},
        format="json",
    )
    assert denied_parent.status_code == 403


@pytest.mark.django_db
def test_department_inbox(ghis, in_ghis, parent):
    diya = Student.objects.get(full_name="Diya Iyer")
    response = parent.post("/api/v1/chat/conversations", {"kind": "department", "department": "accounts", "student_id": str(diya.id)}, format="json")
    assert response.status_code == 201
    assert response.json()["title"] == "Accounts"


@pytest.mark.django_db
def test_non_member_cannot_read_conversation(ghis, in_ghis, parent):
    conversation_id = parent.get("/api/v1/chat/conversations").json()["conversations"][0]["id"]
    outsider = api("+919900000100", ghis)
    assert outsider.get(f"/api/v1/chat/conversations/{conversation_id}/messages").status_code == 404
    assert Notification.objects.filter(user=user(PARENT_MEERA)).exists()
