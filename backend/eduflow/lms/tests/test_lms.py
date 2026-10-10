"""LMS: lessons (video links, notes, files), draft visibility, monotonic progress, quizzes with hidden
keys and best scores, attempt limits, learning paths, live classes, AI drafting only with a provider,
analytics, isolation and RLS."""

import datetime
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.lms.models import LearningPath, Lesson, LiveClass, Quiz
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


def _lesson(client, world, expect=201, **extra):
    body = {
        "section_id": str(world.section_a.pk),
        "subject_id": str(world.subject.pk),
        "title": "Fractions",
        "kind": "video",
        "video_url": "https://video.example/fractions",
        **extra,
    }
    return _ok(client.post("/api/v1/lms/lessons", body, format="json"), expect)


def _quiz(client, world, expect=201, **extra):
    body = {
        "section_id": str(world.section_a.pk),
        "subject_id": str(world.subject.pk),
        "title": "Fractions check",
        "questions": [
            {
                "text": "1/2 + 1/4?",
                "options": ["3/4", "2/6"],
                "answer": 0,
                "explanation": "Common denominator 4.",
            },
            {"text": "1/3 of 9?", "options": ["2", "3", "4"], "answer": 1},
        ],
        **extra,
    }
    return _ok(client.post("/api/v1/lms/quizzes", body, format="json"), expect)


def test_lessons_publish_and_progress(world, as_member):
    teacher, student, parent = (
        as_member(world.teacher),
        as_member(world.student_member),
        as_member(world.parent),
    )
    _lesson(teacher, world, section_id=str(world.section_b.pk), expect=403)
    _lesson(teacher, world, video_url="", expect=400)
    _lesson(teacher, world, video_url="http://insecure.example/x", expect=400)
    _lesson(teacher, world, kind="notes", video_url="", expect=400)
    lesson = _lesson(teacher, world)
    assert _ok(student.get("/api/v1/lms/lessons"))["results"] == []  # draft
    _ok(teacher.patch(f"/api/v1/lms/lessons/{lesson['id']}", {"status": "published"}, format="json"))
    assert Notification.objects.filter(recipient=world.parent, kind="learning").exists()
    assert [row["id"] for row in _ok(parent.get("/api/v1/lms/lessons"))["results"]] == [lesson["id"]]
    _ok(student.post(f"/api/v1/lms/lessons/{lesson['id']}/progress", {"percent": 60}, format="json"))
    assert (
        _ok(student.post(f"/api/v1/lms/lessons/{lesson['id']}/progress", {"percent": 30}, format="json"))[
            "percent"
        ]
        == 60
    )
    done = _ok(student.post(f"/api/v1/lms/lessons/{lesson['id']}/progress", {"percent": 100}, format="json"))
    assert done["completed_at"] is not None
    _ok(parent.post(f"/api/v1/lms/lessons/{lesson['id']}/progress", {"percent": 10}, format="json"), 403)
    assert [p["percent"] for p in _ok(teacher.get(f"/api/v1/lms/lessons/{lesson['id']}/progress"))] == [100]
    notes = _ok(
        teacher.post(
            "/api/v1/lms/lessons",
            {
                "section_id": str(world.section_a.pk),
                "subject_id": str(world.subject.pk),
                "title": "Notes",
                "kind": "notes",
                "upload": SimpleUploadedFile("n.pdf", b"%PDF-1.4 notes"),
            },
            format="multipart",
        ),
        201,
    )
    assert b"".join(teacher.get(f"/api/v1/lms/lessons/{notes['id']}/file")) == b"%PDF-1.4 notes"


def test_quizzes_hide_keys_and_keep_the_best_score(world, as_member):
    teacher, student = as_member(world.teacher), as_member(world.student_member)
    _quiz(teacher, world, questions=[{"text": "x", "options": ["a", "b"], "answer": 5}], expect=400)
    quiz = _quiz(teacher, world, max_attempts=2)
    assert quiz["questions"][0]["answer"] == 0  # the author sees keys
    _ok(
        student.post(f"/api/v1/lms/quizzes/{quiz['id']}/attempts", {"answers": [0, 1]}, format="json"), 404
    )  # draft
    _ok(teacher.patch(f"/api/v1/lms/quizzes/{quiz['id']}", {"status": "published"}, format="json"))
    before = _ok(student.get(f"/api/v1/lms/quizzes/{quiz['id']}"))
    assert {q["answer"] for q in before["questions"]} == {None}
    _ok(student.post(f"/api/v1/lms/quizzes/{quiz['id']}/attempts", {"answers": [0]}, format="json"), 400)
    first = _ok(
        student.post(f"/api/v1/lms/quizzes/{quiz['id']}/attempts", {"answers": [1, -1]}, format="json"), 201
    )
    assert (first["score"], first["total"]) == (0, 2)
    after = _ok(student.get(f"/api/v1/lms/quizzes/{quiz['id']}"))
    assert after["questions"][0]["explanation"] == "Common denominator 4."
    _ok(student.post(f"/api/v1/lms/quizzes/{quiz['id']}/attempts", {"answers": [0, 1]}, format="json"), 201)
    _ok(student.post(f"/api/v1/lms/quizzes/{quiz['id']}/attempts", {"answers": [0, 1]}, format="json"), 409)
    _ok(
        teacher.patch(
            f"/api/v1/lms/quizzes/{quiz['id']}",
            {"questions": [{"text": "y", "options": ["a", "b"], "answer": 0}]},
            format="json",
        ),
        409,
    )
    assert {q["answer"] for q in _ok(teacher.get("/api/v1/lms/quizzes"))["results"][0]["questions"]} == {None}
    lesson = _lesson(teacher, world, kind="quiz", video_url="", quiz_id=quiz["id"])
    _ok(teacher.patch(f"/api/v1/lms/lessons/{lesson['id']}", {"status": "published"}, format="json"))
    analytics = _ok(teacher.get(f"/api/v1/lms/analytics?section_id={world.section_a.pk}"))
    assert analytics["quizzes"][0]["average_best_percent"] == 100.0
    assert analytics["students"] == 1
    _ok(as_member(world.parent).get(f"/api/v1/lms/analytics?section_id={world.section_a.pk}"), 403)
    _ok(teacher.get(f"/api/v1/lms/analytics?section_id={world.section_b.pk}"), 404)


def test_learning_paths(world, as_member):
    teacher, student = as_member(world.teacher), as_member(world.student_member)
    a, b = _lesson(teacher, world), _lesson(teacher, world, title="Decimals")
    for lesson in (a, b):
        _ok(teacher.patch(f"/api/v1/lms/lessons/{lesson['id']}", {"status": "published"}, format="json"))
    path = _ok(
        teacher.post(
            "/api/v1/lms/paths",
            {"section_id": str(world.section_a.pk), "title": "Numbers", "lesson_ids": [a["id"], b["id"]]},
            format="json",
        ),
        201,
    )
    _ok(
        teacher.post(
            "/api/v1/lms/paths",
            {"section_id": str(world.section_a.pk), "title": "Dup", "lesson_ids": [a["id"], a["id"]]},
            format="json",
        ),
        400,
    )
    _ok(teacher.patch(f"/api/v1/lms/paths/{path['id']}", {"status": "published"}, format="json"))
    _ok(student.post(f"/api/v1/lms/lessons/{a['id']}/progress", {"percent": 100}, format="json"))
    assert _ok(student.get(f"/api/v1/lms/paths/{path['id']}/progress")) == [
        {
            "student": {"id": str(world.student.pk), "full_name": "Asha"},
            "steps": 2,
            "completed": 1,
            "percent": 50,
        }
    ]


def test_live_classes(world, as_member):
    teacher, student = as_member(world.teacher), as_member(world.student_member)
    body = {
        "section_id": str(world.section_a.pk),
        "subject_id": str(world.subject.pk),
        "title": "Doubt clearing",
        "starts_at": (timezone.now() + datetime.timedelta(hours=2)).isoformat(),
        "duration_minutes": 40,
        "provider": "Any meeting service",
        "join_url": "https://meet.example/abc",
    }
    live = _ok(teacher.post("/api/v1/lms/live-classes", body, format="json"), 201)
    assert "join_url" not in live
    assert (
        _ok(student.post(f"/api/v1/lms/live-classes/{live['id']}/join"))["join_url"]
        == "https://meet.example/abc"
    )
    _ok(teacher.patch(f"/api/v1/lms/live-classes/{live['id']}", {"status": "cancelled"}, format="json"))
    _ok(student.post(f"/api/v1/lms/live-classes/{live['id']}/join"), 404)  # cancelled classes are hidden


class FakeProvider:
    def generate(self, *, subject, topic, count, grade):
        return [
            {"text": f"{topic} question", "options": ["a", "b"], "answer": 1, "explanation": "because"},
            {"text": "broken", "options": ["only one"], "answer": 0},
        ]


def test_ai_drafting_needs_a_provider(world, as_member, settings):
    teacher = as_member(world.teacher)
    body = {"subject": "Mathematics", "topic": "Fractions", "grade": "5", "count": 2}
    response = teacher.post("/api/v1/lms/quizzes/generate", body, format="json")
    assert (response.status_code, response.json()["error"]["code"]) == (503, "ai_unavailable")
    settings.AI_QUESTION_PROVIDER = "eduflow.lms.tests.test_lms.FakeProvider"
    drafted = _ok(teacher.post("/api/v1/lms/quizzes/generate", body, format="json"))
    assert [q["text"] for q in drafted["questions"]] == ["Fractions question"]  # malformed output dropped
    assert not Quiz.objects.exists()
    assert (
        as_member(world.parent).post("/api/v1/lms/quizzes/generate", body, format="json").status_code == 403
    )


LMS_MATRIX = [
    ("get", "/api/v1/lms/lessons/{lesson}", None),
    ("patch", "/api/v1/lms/lessons/{lesson}", {"title": "x"}),
    ("get", "/api/v1/lms/lessons/{lesson}/file", None),
    ("get", "/api/v1/lms/lessons/{lesson}/progress", None),
    ("post", "/api/v1/lms/lessons/{lesson}/progress", {"percent": 1}),
    ("get", "/api/v1/lms/quizzes/{quiz}", None),
    ("patch", "/api/v1/lms/quizzes/{quiz}", {"title": "x"}),
    ("get", "/api/v1/lms/quizzes/{quiz}/attempts", None),
    ("post", "/api/v1/lms/quizzes/{quiz}/attempts", {"answers": [0]}),
    ("get", "/api/v1/lms/paths/{path}", None),
    ("patch", "/api/v1/lms/paths/{path}", {"title": "x"}),
    ("get", "/api/v1/lms/paths/{path}/progress", None),
    ("get", "/api/v1/lms/live-classes/{live}", None),
    ("patch", "/api/v1/lms/live-classes/{live}", {"title": "x"}),
    ("post", "/api/v1/lms/live-classes/{live}/join", None),
]
LMS_MATRIX_PATHS = {p for _, p, _ in LMS_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), LMS_MATRIX)
def test_another_schools_learning_answers_like_unknown(world, other_world, as_member, method, path, body):
    s, sec, sub = other_world.school, other_world.section_a, other_world.subject
    real = {
        "lesson": Lesson.objects.create(
            school=s, section=sec, subject=sub, title="L", kind="notes", body="b", status="published"
        ).pk,
        "quiz": Quiz.objects.create(school=s, section=sec, subject=sub, title="Q", status="published").pk,
        "path": LearningPath.objects.create(school=s, section=sec, title="P", status="published").pk,
        "live": LiveClass.objects.create(
            school=s,
            section=sec,
            subject=sub,
            title="C",
            starts_at=timezone.now(),
            duration_minutes=10,
            provider="x",
            join_url="https://x.example",
        ).pk,
    }
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(real).status_code == call(missing).status_code == 404
    assert Lesson.objects.get(pk=real["lesson"]).title == "L"


def test_lms_is_under_rls(world, other_world):
    Quiz.objects.create(
        school=other_world.school, section=other_world.section_a, subject=other_world.subject, title="Q"
    )
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Quiz.objects.exists()
