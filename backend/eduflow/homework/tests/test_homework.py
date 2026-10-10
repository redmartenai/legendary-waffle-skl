"""Homework: narrow teacher writes, student submissions (late after the due date), review, the status board,
family notifications, attachments, isolation and RLS."""

import datetime
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from eduflow.audit.models import AuditEvent
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.homework.models import Homework, Submission
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 7, 14)


@pytest.fixture(autouse=True)
def _clock(time_machine):
    time_machine.move_to(datetime.datetime(2026, 7, 14, 4, 0, tzinfo=datetime.UTC))


def _set(client, world, expect=201, **extra):
    body = {
        "section_id": str(world.section_a.pk),
        "subject_id": str(world.subject.pk),
        "title": "Fractions worksheet",
        "due_date": "2026-07-16",
        "max_score": "10",
        **extra,
    }
    response = client.post("/api/v1/homework", body, format="json")
    assert response.status_code == expect, response.content
    return response.json()


def test_a_teacher_sets_homework_for_a_section_they_teach(world, as_member):
    teacher = as_member(world.teacher)
    hw = _set(teacher, world)
    assert (hw["assigned_on"], hw["section"]["id"], hw["status"]) == (
        "2026-07-14",
        str(world.section_a.pk),
        "published",
    )
    _set(teacher, world, section_id=str(world.section_b.pk), expect=403)
    _set(teacher, world, due_date="2026-07-01", expect=400)
    assert Notification.objects.filter(recipient=world.parent, kind="homework").count() == 1
    assert Notification.objects.filter(recipient=world.student_member, kind="homework").count() == 1
    assert AuditEvent.objects.filter(action="homework.homework.created").count() == 1


def test_reads_follow_the_section(world, as_member):
    hw = _set(as_member(world.admin), world)
    _set(as_member(world.admin), world, section_id=str(world.section_b.pk))
    for member, count in [("teacher", 1), ("parent", 1), ("student_member", 1), ("principal", 2)]:
        listed = as_member(getattr(world, member)).get("/api/v1/homework").json()["results"]
        assert len(listed) == count, member
    assert as_member(world.parent).get(f"/api/v1/homework/{hw['id']}").status_code == 200


def test_submission_review_and_board(world, as_member, time_machine):
    hw = _set(as_member(world.teacher), world)
    student = as_member(world.student_member)
    assert student.post(f"/api/v1/homework/{hw['id']}/submit", {}, format="json").status_code == 400
    sub = student.post(
        f"/api/v1/homework/{hw['id']}/submit", {"text": "1/2 + 1/4 = 3/4"}, format="json"
    ).json()
    assert sub["status"] == "submitted"
    board = as_member(world.teacher).get(f"/api/v1/homework/{hw['id']}/submissions").json()
    assert {r["student"]: r["status"] for r in board} == {"Asha": "submitted"}
    parent_board = as_member(world.parent).get(f"/api/v1/homework/{hw['id']}/submissions").json()
    assert [r["status"] for r in parent_board] == ["submitted"]
    # Parents and teachers cannot submit.
    assert (
        as_member(world.parent).post(f"/api/v1/homework/{hw['id']}/submit", {"text": "x"}).status_code == 403
    )
    reviewed = as_member(world.teacher).post(
        f"/api/v1/homework/submissions/{sub['id']}/review",
        {"feedback": "Well done", "score": "9"},
        format="json",
    )
    assert reviewed.json()["status"] == "reviewed"
    over = as_member(world.teacher).post(
        f"/api/v1/homework/submissions/{sub['id']}/review", {"score": "11"}, format="json"
    )
    assert over.status_code == 400
    again = student.post(f"/api/v1/homework/{hw['id']}/submit", {"text": "changed"}, format="json")
    assert again.status_code == 409
    assert Notification.objects.filter(recipient=world.parent, title__startswith="Homework reviewed").exists()
    assert (
        as_member(world.parent)
        .post(f"/api/v1/homework/submissions/{sub['id']}/review", {"feedback": "x"}, format="json")
        .status_code
        == 403
    )


def test_a_submission_after_the_due_date_is_late(world, as_member, time_machine):
    hw = _set(as_member(world.teacher), world)
    time_machine.move_to(datetime.datetime(2026, 7, 17, 4, 0, tzinfo=datetime.UTC))
    sub = (
        as_member(world.student_member)
        .post(f"/api/v1/homework/{hw['id']}/submit", {"text": "sorry"}, format="json")
        .json()
    )
    assert sub["status"] == "late"


def test_a_student_outside_the_class_cannot_submit(world, as_member, make_member):
    hw = _set(as_member(world.admin), world, section_id=str(world.section_b.pk))
    assert (
        as_member(world.student_member)
        .post(f"/api/v1/homework/{hw['id']}/submit", {"text": "x"}, format="json")
        .status_code
        == 404
    )  # not even visible


def test_attachments_and_files(world, as_member):
    teacher = as_member(world.teacher)
    hw = teacher.post(
        "/api/v1/homework",
        {
            "section_id": str(world.section_a.pk),
            "subject_id": str(world.subject.pk),
            "title": "Read",
            "due_date": "2026-07-20",
            "attachment": SimpleUploadedFile("sheet.pdf", b"%PDF-1.4 sheet"),
        },
        format="multipart",
    ).json()
    assert hw["attachment"]["filename"] == "sheet.pdf"
    assert (
        b"".join(as_member(world.parent).get(f"/api/v1/homework/{hw['id']}/attachment")) == b"%PDF-1.4 sheet"
    )
    sub = (
        as_member(world.student_member)
        .post(
            f"/api/v1/homework/{hw['id']}/submit",
            {"file": SimpleUploadedFile("answer.txt", b"my answer")},
            format="multipart",
        )
        .json()
    )
    assert b"".join(teacher.get(f"/api/v1/homework/submissions/{sub['id']}/file")) == b"my answer"
    assert teacher.get(f"/api/v1/homework/{_set(teacher, world)['id']}/attachment").status_code == 404


def test_edit_and_archive_are_narrow(world, as_member):
    hw = _set(as_member(world.admin), world, section_id=str(world.section_b.pk))
    assert (
        as_member(world.teacher)
        .patch(f"/api/v1/homework/{hw['id']}", {"title": "x"}, format="json")
        .status_code
        == 404
    )
    own = _set(as_member(world.teacher), world)
    teacher = as_member(world.teacher)
    assert (
        teacher.patch(f"/api/v1/homework/{own['id']}", {"due_date": "2026-07-01"}, format="json").status_code
        == 400
    )
    assert (
        teacher.patch(f"/api/v1/homework/{own['id']}", {"title": "New"}, format="json").json()["title"]
        == "New"
    )
    assert teacher.delete(f"/api/v1/homework/{own['id']}").status_code == 204
    assert Homework.objects.get(pk=own["id"]).status == "archived"
    assert teacher.patch(f"/api/v1/homework/{own['id']}", {"title": "x"}, format="json").status_code == 409
    assert as_member(world.parent).delete(f"/api/v1/homework/{own['id']}").status_code == 403


def test_monitoring_inputs(world, as_member, time_machine):
    from eduflow.homework import services

    hw = _set(as_member(world.teacher), world)
    as_member(world.student_member).post(f"/api/v1/homework/{hw['id']}/submit", {"text": "x"}, format="json")
    assert services.unreviewed_counts(world.school) == {world.teacher_staff.pk: 1}
    assert services.missed_counts(world.school, TODAY, datetime.date(2026, 7, 31)) == {}
    _set(as_member(world.admin), world, section_id=str(world.section_b.pk))
    assert services.missed_counts(world.school, TODAY, datetime.date(2026, 7, 31)) == {
        world.other_student.pk: 1
    }


HOMEWORK_MATRIX = [
    ("get", "/api/v1/homework/{homework}", None),
    ("patch", "/api/v1/homework/{homework}", {"title": "x"}),
    ("delete", "/api/v1/homework/{homework}", None),
    ("get", "/api/v1/homework/{homework}/submissions", None),
    ("post", "/api/v1/homework/{homework}/submit", {"text": "x"}),
    ("get", "/api/v1/homework/{homework}/attachment", None),
    ("post", "/api/v1/homework/submissions/{submission}/review", {"feedback": "x"}),
    ("get", "/api/v1/homework/submissions/{submission}/file", None),
]
HOMEWORK_MATRIX_PATHS = {p for _, p, _ in HOMEWORK_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), HOMEWORK_MATRIX)
def test_another_schools_homework_answers_like_an_unknown_one(
    world, other_world, as_member, method, path, body
):
    foreign = _set(as_member(other_world.teacher), other_world)
    sub = (
        as_member(other_world.student_member)
        .post(f"/api/v1/homework/{foreign['id']}/submit", {"text": "x"}, format="json")
        .json()
    )
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    real = {"homework": foreign["id"], "submission": sub["id"]}
    missing = {"homework": uuid.uuid4(), "submission": uuid.uuid4()}
    assert call(real).status_code == call(missing).status_code == 404
    assert Homework.objects.get(pk=foreign["id"]).title == "Fractions worksheet"


def test_homework_is_under_rls(world, other_world, as_member):
    hw = _set(as_member(other_world.teacher), other_world)
    as_member(other_world.student_member).post(
        f"/api/v1/homework/{hw['id']}/submit", {"text": "x"}, format="json"
    )
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Homework.objects.exists()
        assert not Submission.objects.exists()
