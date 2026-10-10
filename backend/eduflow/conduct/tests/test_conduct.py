"""Remarks and behaviour incidents: narrow writes, family visibility, notifications, resolution, isolation
and RLS."""

import datetime
import uuid

import pytest

from eduflow.conduct import services
from eduflow.conduct.models import Incident, Remark
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db


def _remark(client, student, expect=201, **extra):
    body = {"student_id": str(student.pk), "tone": "praise", "text": "Great effort in class", **extra}
    response = client.post("/api/v1/remarks", body, format="json")
    assert response.status_code == expect, response.content
    return response.json()


def _incident(client, student, expect=201, **extra):
    body = {
        "student_id": str(student.pk),
        "occurred_on": "2026-07-10",
        "category": "Disruption",
        "description": "Talking during the test",
        "severity": "minor",
        **extra,
    }
    response = client.post("/api/v1/incidents", body, format="json")
    assert response.status_code == expect, response.content
    return response.json()


def test_a_teacher_writes_remarks_about_students_they_teach(world, as_member):
    teacher = as_member(world.teacher)
    remark = _remark(teacher, world.student, subject_id=str(world.subject.pk))
    assert (remark["tone"], remark["subject"]["name"]) == ("praise", "Mathematics")
    _remark(teacher, world.other_student, expect=403)
    assert Notification.objects.filter(recipient=world.parent, kind="remark").count() == 1


def test_families_see_only_what_is_visible_to_them(world, as_member):
    teacher = as_member(world.teacher)
    _remark(teacher, world.student)
    private = _remark(teacher, world.student, tone="concern", text="Watch focus", visible_to_family=False)
    assert Notification.objects.filter(recipient=world.parent, kind="remark").count() == 1
    for member in ("parent", "student_member"):
        listed = as_member(getattr(world, member)).get("/api/v1/remarks").json()["results"]
        assert [r["tone"] for r in listed] == ["praise"], member
        assert as_member(getattr(world, member)).get(f"/api/v1/remarks/{private['id']}").status_code == 404
    assert len(teacher.get("/api/v1/remarks").json()["results"]) == 2


def test_edit_and_delete_remarks(world, as_member):
    teacher = as_member(world.teacher)
    remark = _remark(teacher, world.student)
    assert (
        teacher.patch(f"/api/v1/remarks/{remark['id']}", {"text": "Better"}, format="json").json()["text"]
        == "Better"
    )
    assert as_member(world.parent).delete(f"/api/v1/remarks/{remark['id']}").status_code == 403
    assert teacher.delete(f"/api/v1/remarks/{remark['id']}").status_code == 204
    assert not Remark.objects.exists()


def test_incidents_are_reported_and_resolved(world, as_member, time_machine):
    time_machine.move_to(datetime.datetime(2026, 7, 14, 4, 0, tzinfo=datetime.UTC))
    teacher = as_member(world.teacher)
    incident = _incident(teacher, world.student)
    _incident(teacher, world.student, occurred_on="2026-07-20", expect=400)
    _incident(teacher, world.other_student, expect=403)
    assert as_member(world.parent).get("/api/v1/incidents").json()["results"] == []  # not visible to family
    shown = _incident(teacher, world.student, visible_to_family=True, severity="moderate")
    assert [i["id"] for i in as_member(world.parent).get("/api/v1/incidents").json()["results"]] == [
        shown["id"]
    ]
    resolved = teacher.post(
        f"/api/v1/incidents/{incident['id']}/resolve", {"action_taken": "Spoke to student"}
    )
    assert (resolved.json()["status"], resolved.json()["action_taken"]) == ("resolved", "Spoke to student")
    assert teacher.post(f"/api/v1/incidents/{incident['id']}/resolve", {}).status_code == 409
    assert as_member(world.parent).post(f"/api/v1/incidents/{shown['id']}/resolve", {}).status_code == 403
    assert (
        teacher.patch(f"/api/v1/incidents/{shown['id']}", {"severity": "serious"}, format="json").status_code
        == 200
    )
    counts = services.incident_counts(world.school, datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))
    assert counts == {world.student.pk: 2}


CONDUCT_MATRIX = [
    ("get", "/api/v1/remarks/{remark}", None),
    ("patch", "/api/v1/remarks/{remark}", {"text": "x"}),
    ("delete", "/api/v1/remarks/{remark}", None),
    ("get", "/api/v1/incidents/{incident}", None),
    ("patch", "/api/v1/incidents/{incident}", {"category": "x"}),
    ("post", "/api/v1/incidents/{incident}/resolve", {"action_taken": "x"}),
]
CONDUCT_MATRIX_PATHS = {p for _, p, _ in CONDUCT_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), CONDUCT_MATRIX)
def test_another_schools_records_answer_like_unknown_ones(world, other_world, as_member, method, path, body):
    real = {
        "remark": _remark(as_member(other_world.admin), other_world.student)["id"],
        "incident": _incident(as_member(other_world.admin), other_world.student)["id"],
    }
    missing = {"remark": uuid.uuid4(), "incident": uuid.uuid4()}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(real).status_code == call(missing).status_code == 404
    assert Remark.objects.filter(pk=real["remark"]).exists()


def test_conduct_is_under_rls(world, other_world, as_member):
    _remark(as_member(other_world.admin), other_world.student)
    _incident(as_member(other_world.admin), other_world.student)
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Remark.objects.exists()
        assert not Incident.objects.exists()
