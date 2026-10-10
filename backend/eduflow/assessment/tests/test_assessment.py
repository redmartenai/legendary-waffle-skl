"""Exams end to end: sheets, narrow marks entry, submission, approval in the queue, publication to families,
corrections, results, report cards with the school's bands, monitoring inputs, isolation and RLS."""

import datetime
import uuid
from decimal import Decimal

import pytest

from eduflow.assessment import services
from eduflow.assessment.models import Exam, Mark, MarkSheet
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


def _exam(client, world, name="Unit Test 1", start="2026-07-01", end="2026-07-03", deadline="2026-07-08"):
    body = {
        "name": name,
        "academic_year_id": str(world.year.pk),
        "starts_on": start,
        "ends_on": end,
        "marks_deadline": deadline,
    }
    return _ok(client.post("/api/v1/exams", body, format="json"), 201)


@pytest.fixture
def sheet(world, as_member):
    admin = as_member(world.admin)
    exam = _exam(admin, world)
    assert _ok(
        admin.post(
            f"/api/v1/exams/{exam['id']}/sheets/generate",
            {"max_marks": "50", "pass_marks": "17"},
            format="json",
        )
    ) == {"created": 1}
    return _ok(admin.get(f"/api/v1/exams/{exam['id']}/sheets"))[0] | {"exam": exam}


def _enter(client, sheet, entries, expect=200):
    return _ok(
        client.put(f"/api/v1/mark-sheets/{sheet['id']}/marks", {"marks": entries}, format="json"), expect
    )


def _approve(world, as_member, kind, pk):
    _ok(
        as_member(world.principal).post(
            f"/api/v1/approvals/{kind}/{pk}/decision", {"decision": "approve"}, format="json"
        )
    )


def test_exam_validation(world, other_world, as_member):
    admin = as_member(world.admin)
    _exam(admin, world)
    assert (
        admin.post(
            "/api/v1/exams",
            {
                "name": "Unit Test 1",
                "academic_year_id": str(world.year.pk),
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-03",
                "marks_deadline": "2026-07-08",
            },
            format="json",
        ).status_code
        == 409
    )
    bad = admin.post(
        "/api/v1/exams",
        {
            "name": "X",
            "academic_year_id": str(world.year.pk),
            "starts_on": "2026-07-05",
            "ends_on": "2026-07-03",
            "marks_deadline": "2026-07-08",
        },
        format="json",
    )
    assert "ends_on" in bad.json()["error"]["fields"]
    assert as_member(world.teacher).post("/api/v1/exams", {}, format="json").status_code == 403
    assert len(_ok(as_member(world.parent).get("/api/v1/exams"))["results"]) == 1


def test_the_full_marks_lifecycle(world, sheet, as_member):
    teacher = as_member(world.teacher)
    assert sheet["teacher"] is not None
    assert sheet["status"] == "draft"
    _enter(teacher, sheet, [{"student_id": str(world.student.pk), "marks": "51"}], expect=400)
    _enter(
        teacher, sheet, [{"student_id": str(world.other_student.pk), "marks": "10"}], expect=400
    )  # not in 5A
    _enter(teacher, sheet, [{"student_id": str(world.student.pk), "absent": True, "marks": "3"}], expect=400)
    detail = _enter(
        teacher, sheet, [{"student_id": str(world.student.pk), "marks": "42.5", "remark": "Good"}]
    )
    assert [(e["student"]["full_name"], e["marks"]) for e in detail["entries"]] == [("Asha", "42.50")]
    # Families see nothing before publication.
    assert as_member(world.parent).get(f"/api/v1/mark-sheets/{sheet['id']}").status_code == 404
    _ok(teacher.post(f"/api/v1/mark-sheets/{sheet['id']}/submit"))
    _enter(teacher, sheet, [{"student_id": str(world.student.pk), "marks": "40"}], expect=409)
    queue = _ok(as_member(world.principal).get("/api/v1/approvals"))
    assert [i["kind"] for i in queue] == ["mark_sheet"]
    _approve(world, as_member, "mark_sheet", sheet["id"])
    published = _ok(as_member(world.admin).post(f"/api/v1/exams/{sheet['exam']['id']}/publish"))
    assert published == {"published_sheets": 1}
    assert Notification.objects.filter(recipient=world.parent, kind="marks").count() == 1
    parent_view = _ok(as_member(world.parent).get(f"/api/v1/mark-sheets/{sheet['id']}"))
    assert [e["marks"] for e in parent_view["entries"]] == ["42.50"]
    card = _ok(
        as_member(world.student_member).get(
            f"/api/v1/students/{world.student.pk}/report-card?exam_id={sheet['exam']['id']}"
        )
    )
    assert (card["percent"], card["grade"], card["lines"][0]["passed"]) == ("85.00", None, True)


def test_a_returned_sheet_goes_back_to_draft(world, sheet, as_member):
    teacher = as_member(world.teacher)
    _enter(teacher, sheet, [{"student_id": str(world.student.pk), "marks": "20"}])
    _ok(teacher.post(f"/api/v1/mark-sheets/{sheet['id']}/submit"))
    _ok(
        as_member(world.principal).post(
            f"/api/v1/approvals/mark_sheet/{sheet['id']}/decision",
            {"decision": "decline", "note": "Recheck Q3"},
            format="json",
        )
    )
    detail = _ok(teacher.get(f"/api/v1/mark-sheets/{sheet['id']}"))
    assert (detail["status"], detail["return_note"]) == ("draft", "Recheck Q3")
    assert Notification.objects.filter(recipient=world.teacher, title="Mark sheet returned").exists()


def test_submission_needs_every_student(world, sheet, as_member):
    assert as_member(world.teacher).post(f"/api/v1/mark-sheets/{sheet['id']}/submit").status_code == 400


def test_corrections_after_publication(world, sheet, as_member):
    teacher = as_member(world.teacher)
    _enter(teacher, sheet, [{"student_id": str(world.student.pk), "marks": "30"}])
    mark = Mark.objects.get()
    assert (
        teacher.post(
            f"/api/v1/marks/{mark.pk}/corrections", {"new_marks": "35", "reason": "x"}, format="json"
        ).status_code
        == 409
    )
    _ok(teacher.post(f"/api/v1/mark-sheets/{sheet['id']}/submit"))
    _approve(world, as_member, "mark_sheet", sheet["id"])
    _ok(as_member(world.admin).post(f"/api/v1/exams/{sheet['exam']['id']}/publish"))
    correction = _ok(
        teacher.post(
            f"/api/v1/marks/{mark.pk}/corrections",
            {"new_marks": "35", "reason": "Totalling error"},
            format="json",
        ),
        201,
    )
    assert (
        teacher.post(
            f"/api/v1/marks/{mark.pk}/corrections", {"new_marks": "36", "reason": "again"}, format="json"
        ).status_code
        == 409
    )
    _approve(world, as_member, "marks_correction", correction["id"])
    mark.refresh_from_db()
    assert mark.marks == Decimal("35.00")
    assert (
        as_member(world.parent)
        .post(f"/api/v1/marks/{mark.pk}/corrections", {"new_marks": "50", "reason": "x"}, format="json")
        .status_code
        == 403
    )


def test_teachers_work_only_on_their_sheets(world, as_member):
    admin = as_member(world.admin)
    exam = _exam(admin, world)
    other = _ok(
        admin.post(
            f"/api/v1/exams/{exam['id']}/sheets",
            {"section_id": str(world.section_b.pk), "subject_id": str(world.subject.pk), "max_marks": "100"},
            format="json",
        ),
        201,
    )
    assert (
        admin.post(
            f"/api/v1/exams/{exam['id']}/sheets",
            {"section_id": str(world.section_b.pk), "subject_id": str(world.subject.pk), "max_marks": "100"},
            format="json",
        ).status_code
        == 409
    )
    teacher = as_member(world.teacher)
    assert teacher.get(f"/api/v1/mark-sheets/{other['id']}").status_code == 404
    _enter(teacher, other, [{"student_id": str(world.other_student.pk), "marks": "1"}], expect=404)
    _enter(admin, other, [{"student_id": str(world.other_student.pk), "marks": "61"}])  # office: school-wide


def test_results_ranks_and_bands(world, sheet, as_member):
    from eduflow.people.models import Enrollment, Student

    bala = Student.objects.create(school=world.school, admission_number="S-3", first_name="Bala")
    Enrollment.objects.create(
        school=world.school,
        student=bala,
        academic_year=world.year,
        grade=world.grade,
        section=world.section_a,
        start_date=world.year.start_date,
    )
    admin = as_member(world.admin)
    for label, minimum in [("A", "80"), ("B", "60"), ("C", "0")]:
        _ok(admin.post("/api/v1/grade-bands", {"label": label, "min_percent": minimum}, format="json"), 201)
    _enter(
        as_member(world.teacher),
        sheet,
        [{"student_id": str(world.student.pk), "marks": "35"}, {"student_id": str(bala.pk), "marks": "45"}],
    )
    rows = _ok(admin.get(f"/api/v1/exams/{sheet['exam']['id']}/results?section_id={world.section_a.pk}"))
    assert [(r["student"]["full_name"], r["percent"], r["grade"], r["rank"]) for r in rows] == [
        ("Bala", "90.00", "A", 1),
        ("Asha", "70.00", "B", 2),
    ]
    assert (
        _ok(
            as_member(world.parent).get(
                f"/api/v1/exams/{sheet['exam']['id']}/results?section_id={world.section_a.pk}"
            )
        )
        == []
    )


def test_monitoring_inputs(world, as_member):
    admin, teacher = as_member(world.admin), as_member(world.teacher)
    first = _exam(admin, world)
    second = _exam(
        admin, world, name="Unit Test 2", start="2026-08-01", end="2026-08-02", deadline="2026-08-05"
    )
    for exam, marks in [(first, "40"), (second, "30")]:
        _ok(admin.post(f"/api/v1/exams/{exam['id']}/sheets/generate", {"max_marks": "50"}, format="json"))
        sheet = MarkSheet.objects.get(exam_id=exam["id"])
        _enter(teacher, {"id": sheet.pk}, [{"student_id": str(world.student.pk), "marks": marks}])
    MarkSheet.objects.filter(exam_id=first["id"]).update(status="submitted", submitted_at="2026-07-07T10:00Z")
    assert list(services.overdue_sheets(world.school, datetime.date(2026, 8, 10))) == [
        MarkSheet.objects.get(exam_id=second["id"])
    ]
    MarkSheet.objects.filter(exam_id=second["id"]).update(
        status="submitted", submitted_at="2026-08-04T10:00Z"
    )
    series = services.exam_percentages(world.school)[world.student.pk]
    assert services.trend(series) == (Decimal(80), Decimal(60), Decimal(-20))


ASSESSMENT_MATRIX = [
    ("get", "/api/v1/exams/{exam}", None),
    ("patch", "/api/v1/exams/{exam}", {"name": "x"}),
    ("get", "/api/v1/exams/{exam}/sheets", None),
    (
        "post",
        "/api/v1/exams/{exam}/sheets",
        {"section_id": "{section}", "subject_id": "{subject}", "max_marks": "10"},
    ),
    ("post", "/api/v1/exams/{exam}/sheets/generate", {"max_marks": "10"}),
    ("post", "/api/v1/exams/{exam}/publish", None),
    ("get", "/api/v1/exams/{exam}/results?section_id={section}", None),
    ("get", "/api/v1/mark-sheets/{sheet}", None),
    ("put", "/api/v1/mark-sheets/{sheet}/marks", {"marks": [{"student_id": "{student}", "marks": "1"}]}),
    ("post", "/api/v1/mark-sheets/{sheet}/submit", None),
    ("post", "/api/v1/marks/{mark}/corrections", {"new_marks": "2", "reason": "x"}),
    ("get", "/api/v1/students/{student}/report-card?exam_id={exam}", None),
    ("get", "/api/v1/grade-bands/{band}", None),
    ("patch", "/api/v1/grade-bands/{band}", {"label": "Z"}),
    ("delete", "/api/v1/grade-bands/{band}", None),
]
ASSESSMENT_MATRIX_PATHS = {p.split("?")[0] for _, p, _ in ASSESSMENT_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), ASSESSMENT_MATRIX)
def test_another_schools_exam_data_answers_like_unknown(world, other_world, as_member, method, path, body):
    admin_b = as_member(other_world.admin)
    exam = _exam(admin_b, other_world)
    _ok(admin_b.post(f"/api/v1/exams/{exam['id']}/sheets/generate", {"max_marks": "50"}, format="json"))
    sheet = MarkSheet.objects.get(exam_id=exam["id"])
    _enter(admin_b, {"id": sheet.pk}, [{"student_id": str(other_world.student.pk), "marks": "5"}])
    band = _ok(admin_b.post("/api/v1/grade-bands", {"label": "A", "min_percent": "80"}, format="json"), 201)
    real = {
        "exam": exam["id"],
        "sheet": sheet.pk,
        "mark": Mark.objects.get().pk,
        "band": band["id"],
        "student": other_world.student.pk,
        "section": world.section_a.pk,
        "subject": world.subject.pk,
    }
    missing = {**real, **{k: uuid.uuid4() for k in ("exam", "sheet", "mark", "band", "student")}}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        payload = None
        if body:
            payload = {
                k: (
                    v.format(**values)
                    if isinstance(v, str)
                    else [{kk: vv.format(**values) for kk, vv in e.items()} for e in v]
                )
                for k, v in body.items()
            }
        return (
            getattr(client, method)(url, payload, format="json") if payload else getattr(client, method)(url)
        )

    assert call(real).status_code in (400, 404)
    assert call(real).status_code == call(missing).status_code
    assert Exam.objects.get(pk=exam["id"]).name == "Unit Test 1"


def test_assessment_is_under_rls(world, other_world, as_member):
    _exam(as_member(other_world.admin), other_world)
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Exam.objects.exists()
