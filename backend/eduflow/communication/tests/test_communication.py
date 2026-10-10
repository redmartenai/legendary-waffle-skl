"""Communication: addressed announcements with acknowledgements and staff responses, teacher section
announcements, parent-teacher threads with reply tracking, complaints with reporter sentiment, isolation
and RLS."""

import datetime
import uuid

import pytest
from django.utils import timezone

from eduflow.communication import services
from eduflow.communication.models import Announcement, Complaint, Message, Thread
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


def _announce(client, expect=201, **extra):
    body = {"title": "Sports day", "body": "Friday 9am", "audiences": ["parents", "staff"], **extra}
    return _ok(client.post("/api/v1/announcements", body, format="json"), expect)


def test_announcements_reach_their_audience(world, as_member):
    admin = as_member(world.admin)
    ann = _announce(admin)
    assert Notification.objects.filter(recipient=world.parent, kind="announcement").count() == 1
    assert Notification.objects.filter(recipient=world.teacher, kind="announcement").count() == 1
    assert not Notification.objects.filter(recipient=world.student_member).exists()
    assert [a["id"] for a in _ok(as_member(world.parent).get("/api/v1/announcements"))["results"]] == [
        ann["id"]
    ]
    assert _ok(as_member(world.student_member).get("/api/v1/announcements"))["results"] == []
    parent = as_member(world.parent)
    assert [a["id"] for a in _ok(parent.get("/api/v1/announcements/pending"))] == [ann["id"]]
    _ok(parent.post(f"/api/v1/announcements/{ann['id']}/acknowledge"), 204)
    _ok(parent.post(f"/api/v1/announcements/{ann['id']}/acknowledge"), 204)  # idempotent
    assert _ok(parent.get("/api/v1/announcements/pending")) == []
    _ok(parent.post(f"/api/v1/announcements/{ann['id']}/responses", {"text": "x"}, format="json"), 403)
    _ok(
        as_member(world.teacher).post(
            f"/api/v1/announcements/{ann['id']}/responses", {"text": "I will lead the relay"}, format="json"
        ),
        201,
    )
    status = _ok(admin.get(f"/api/v1/announcements/{ann['id']}/acknowledgements"))
    assert (status["recipients"], status["acknowledged"]) == (2, 1)
    assert (
        _ok(admin.get(f"/api/v1/announcements/{ann['id']}"))["responses"][0]["text"]
        == "I will lead the relay"
    )
    _ok(admin.delete(f"/api/v1/announcements/{ann['id']}"), 204)
    assert _ok(parent.get("/api/v1/announcements"))["results"] == []


def test_teachers_announce_to_their_sections(world, as_member):
    teacher = as_member(world.teacher)
    _announce(teacher, expect=403)  # school-wide
    _announce(teacher, section_id=str(world.section_b.pk), expect=403)
    ann = _announce(teacher, section_id=str(world.section_a.pk), audiences=["parents", "students"])
    assert Notification.objects.filter(recipient=world.student_member, kind="announcement").exists()
    assert [
        a["id"] for a in _ok(as_member(world.student_member).get("/api/v1/announcements"))["results"]
    ] == [ann["id"]]
    _ok(as_member(world.parent).delete(f"/api/v1/announcements/{ann['id']}"), 403)


def test_parent_teacher_threads_track_replies(world, as_member):
    parent, teacher = as_member(world.parent), as_member(world.teacher)
    thread = _ok(
        parent.post(
            "/api/v1/threads",
            {"student_id": str(world.student.pk), "staff_id": str(world.teacher_staff.pk)},
            format="json",
        ),
        201,
    )
    assert (thread["kind"], thread["subject"]["name"]) == ("parent_subject_teacher", "Mathematics")
    again = _ok(
        parent.post(
            "/api/v1/threads",
            {"student_id": str(world.student.pk), "staff_id": str(world.teacher_staff.pk)},
            format="json",
        ),
        201,
    )
    assert again["id"] == thread["id"]
    _ok(
        parent.post(
            "/api/v1/threads",
            {"student_id": str(world.other_student.pk), "staff_id": str(world.teacher_staff.pk)},
            format="json",
        ),
        400,
    )
    _ok(
        parent.post(
            f"/api/v1/threads/{thread['id']}/messages",
            {"text": "How is Asha doing in fractions?"},
            format="json",
        ),
        201,
    )
    assert Notification.objects.filter(recipient=world.teacher, kind="message").exists()
    assert _ok(teacher.get("/api/v1/threads?awaiting_reply=true"))["results"][0]["id"] == thread["id"]
    Thread.objects.filter(pk=thread["id"]).update(
        awaiting_reply_since=timezone.now() - datetime.timedelta(hours=30)
    )
    assert [t.pk for t in services.unanswered(world.school)] == [uuid.UUID(thread["id"])]
    _ok(teacher.post(f"/api/v1/threads/{thread['id']}/messages", {"text": "Very well."}, format="json"), 201)
    assert list(services.unanswered(world.school)) == []
    hours = services.reply_hours(world.school, timezone.now() - datetime.timedelta(days=1))
    assert round(hours[world.teacher_staff.pk]) == 30
    assert [m["from_staff"] for m in _ok(parent.get(f"/api/v1/threads/{thread['id']}/messages"))] == [
        False,
        True,
    ]
    assert as_member(world.student_member).get(f"/api/v1/threads/{thread['id']}").status_code == 404
    # The principal may read every thread but does not write in other people's conversations.
    principal = as_member(world.principal)
    assert principal.get(f"/api/v1/threads/{thread['id']}/messages").status_code == 200
    _ok(principal.post(f"/api/v1/threads/{thread['id']}/messages", {"text": "x"}, format="json"), 403)
    student_thread = _ok(
        as_member(world.student_member).post(
            "/api/v1/threads",
            {"student_id": str(world.student.pk), "staff_id": str(world.teacher_staff.pk)},
            format="json",
        ),
        201,
    )
    assert student_thread["kind"] == "student_teacher"


def test_complaints_and_sentiment(world, as_member):
    parent, admin = as_member(world.parent), as_member(world.admin)
    complaint = _ok(
        parent.post(
            "/api/v1/complaints",
            {
                "student_id": str(world.student.pk),
                "category": "transport",
                "text": "Bus late again",
                "sentiment": "negative",
            },
            format="json",
        ),
        201,
    )
    _ok(
        parent.post(
            "/api/v1/complaints",
            {
                "student_id": str(world.other_student.pk),
                "category": "food",
                "text": "x",
                "sentiment": "neutral",
            },
            format="json",
        ),
        400,
    )
    _ok(
        parent.post(
            "/api/v1/complaints",
            {
                "student_id": str(world.student.pk),
                "category": "academics",
                "text": "Great teachers",
                "sentiment": "positive",
            },
            format="json",
        ),
        201,
    )
    summary = _ok(admin.get("/api/v1/complaints/sentiment"))
    assert summary["by_sentiment"] == {"negative": 1, "neutral": 0, "positive": 1}
    assert summary["by_category"]["transport"] == {"total": 1, "open_negative": 1}
    assert summary["net_sentiment"] == 0.0
    _ok(admin.patch(f"/api/v1/complaints/{complaint['id']}", {"status": "resolved"}, format="json"), 400)
    _ok(
        admin.patch(
            f"/api/v1/complaints/{complaint['id']}",
            {"status": "resolved", "resolution": "Route changed"},
            format="json",
        )
    )
    assert Notification.objects.filter(recipient=world.parent, title__contains="resolved").exists()
    _ok(admin.patch(f"/api/v1/complaints/{complaint['id']}", {"status": "open"}, format="json"), 409)
    _ok(parent.patch(f"/api/v1/complaints/{complaint['id']}", {"status": "open"}, format="json"), 403)
    assert as_member(world.teacher).get("/api/v1/complaints").status_code == 403
    assert as_member(world.parent).get("/api/v1/complaints/sentiment").status_code == 403


COMMUNICATION_MATRIX = [
    ("get", "/api/v1/announcements/{announcement}", None),
    ("patch", "/api/v1/announcements/{announcement}", {"title": "x"}),
    ("delete", "/api/v1/announcements/{announcement}", None),
    ("post", "/api/v1/announcements/{announcement}/acknowledge", None),
    ("post", "/api/v1/announcements/{announcement}/responses", {"text": "x"}),
    ("get", "/api/v1/announcements/{announcement}/acknowledgements", None),
    ("get", "/api/v1/threads/{thread}", None),
    ("get", "/api/v1/threads/{thread}/messages", None),
    ("post", "/api/v1/threads/{thread}/messages", {"text": "x"}),
    ("get", "/api/v1/complaints/{complaint}", None),
    ("patch", "/api/v1/complaints/{complaint}", {"status": "in_progress"}),
]
COMMUNICATION_MATRIX_PATHS = {p for _, p, _ in COMMUNICATION_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), COMMUNICATION_MATRIX)
def test_another_schools_communication_answers_like_unknown(
    world, other_world, as_member, method, path, body
):
    s = other_world.school
    real = {
        "announcement": Announcement.objects.create(school=s, title="A", body="b", audiences=["staff"]).pk,
        "thread": Thread.objects.create(
            school=s,
            kind="parent_class_teacher",
            student=other_world.student,
            staff=other_world.teacher_staff,
            guardian=other_world.guardian,
        ).pk,
        "complaint": Complaint.objects.create(
            school=s,
            student=other_world.student,
            category="food",
            text="x",
            sentiment="neutral",
            raised_by=other_world.parent,
        ).pk,
    }
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(real).status_code == call(missing).status_code == 404
    assert not Message.objects.exists()


def test_communication_is_under_rls(world, other_world):
    Announcement.objects.create(school=other_world.school, title="A", body="b", audiences=["staff"])
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Announcement.objects.exists()
