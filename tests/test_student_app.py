"""Student app: materials, classes shelf, assignments with milestones, exam date sheet, ID card, homework."""

from datetime import date, time, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.academics.models import Student, Subject
from apps.homework.models import Homework
from apps.learning.models import Assignment, AssignmentGroup, AssignmentMilestone, MilestoneProgress, StudyMaterial, SyllabusProgress
from apps.results.models import Exam, ExamPaper, PrepItem

from .conftest import PARENT_MEERA, PARENT_RAHUL, STUDENT_KABIR, TEACHER_ANITA, api, user


@pytest.fixture
def kabir(in_ghis):
    return Student.objects.get(full_name="Kabir Sharma")


@pytest.fixture
def student(kabir):
    return api(STUDENT_KABIR)


@pytest.mark.django_db
def test_materials_and_new_counts(student, kabir):
    maths = Subject.objects.get(code="MATH")
    StudyMaterial.objects.create(class_group=kabir.class_group, subject=maths, kind="notes", title="Quadratics", published_at=timezone.now(), author=user(TEACHER_ANITA))
    StudyMaterial.objects.create(class_group=kabir.class_group, subject=maths, kind="video", title="Old", published_at=timezone.now() - timedelta(days=30))
    body = student.get(f"/api/v1/students/{kabir.id}/materials").json()
    assert body["new_total"] == 1
    assert {m["title"]: m["new"] for m in body["items"]} == {"Quadratics": True, "Old": False}
    assert student.get(f"/api/v1/students/{kabir.id}/materials", {"subject": "SCI"}).json()["items"] == []
    # Another family's parent can't see Kabir's class material.
    assert api(PARENT_MEERA).get(f"/api/v1/students/{kabir.id}/materials").status_code == 404


@pytest.mark.django_db
def test_classes_shelf(student, kabir):
    maths = Subject.objects.get(code="MATH")
    SyllabusProgress.objects.create(class_group=kabir.class_group, subject=maths, percent=62)
    body = student.get(f"/api/v1/students/{kabir.id}/classes").json()
    row = next(s for s in body["subjects"] if s["subject"]["code"] == "MATH")
    assert row["syllabus"] == 62 and row["teacher"]
    assert row["next"] is not None


@pytest.mark.django_db
def test_group_assignment_milestones_and_upload(student, kabir):
    sci = Subject.objects.get(code="SCI")
    other = Student.objects.filter(class_group=kabir.class_group).exclude(id=kabir.id).first()
    a = Assignment.objects.create(class_group=kabir.class_group, subject=sci, kind="project", title="Model", group_size=2, due_date=date.today() + timedelta(days=10))
    g = AssignmentGroup.objects.create(assignment=a)
    g.members.add(kabir, other)
    m = AssignmentMilestone.objects.create(assignment=a, title="Research", due_date=date.today(), order=1)
    progress = MilestoneProgress.objects.create(group=g, milestone=m)
    progress.owners.add(kabir)
    body = student.get(f"/api/v1/students/{kabir.id}/assignments").json()
    item = body["in_progress"][0]
    assert item["group"]["members"][0]["first_name"] and item["milestones"][0]["owners"][0]["me"] is True
    assert student.post(f"/api/v1/assignments/milestones/{progress.id}/toggle", {"done": True}, format="json").json()["done_at"]
    # A parent can't tick a milestone; only group members.
    assert api(PARENT_RAHUL).post(f"/api/v1/assignments/milestones/{progress.id}/toggle", {"done": True}, format="json").status_code == 403
    photo = SimpleUploadedFile("model.jpg", b"\xff\xd8\xff" + b"0" * 100, content_type="image/jpeg")
    up = student.post(f"/api/v1/assignments/{a.id}/files", {"student_id": str(kabir.id), "files": [photo]}, format="multipart")
    assert up.status_code == 201 and up.json()["submission"]["files"][0]["name"] == "model.jpg"


@pytest.mark.django_db
def test_locked_assignment_hides_details(student, kabir):
    eng = Subject.objects.get(code="ENG")
    Assignment.objects.create(class_group=kabir.class_group, subject=eng, title="Book review", description="Secret brief", teaser="300 words", opens_on=date.today() + timedelta(days=3), due_date=date.today() + timedelta(days=20))
    body = student.get(f"/api/v1/students/{kabir.id}/assignments").json()
    assert body["upcoming"][0]["locked"] is True and body["upcoming"][0]["description"] == ""
    assert body["upcoming"][0]["teaser"] == "300 words"


@pytest.mark.django_db
def test_exam_datesheet_prep_and_pdf(student, kabir):
    exam = Exam.objects.create(class_group=kabir.class_group, name="Half-yearly", held_on=date.today() + timedelta(days=20), admit_cards_from=date.today() + timedelta(days=13), report_by=time(8, 45))
    ExamPaper.objects.create(exam=exam, subject=Subject.objects.get(code="ENG"), date=exam.held_on, starts_at=time(9), ends_at=time(11, 30), room="Room 305", syllabus=["Ch 1–5"])
    item = PrepItem.objects.create(exam=exam, student=kabir, title="Check the date sheet", due_date=date.today())
    body = student.get(f"/api/v1/students/{kabir.id}/exams").json()
    assert body["exam"]["admit_card_available"] is False and body["papers"][0]["syllabus"] == ["Ch 1–5"]
    assert student.post(f"/api/v1/exams/prep/{item.id}/toggle", {"done": True}, format="json").json()["done_at"]
    pdf = student.get("/api/v1" + body["exam"]["datesheet"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # The admit card stays locked until the release date.
    assert student.get("/api/v1" + body["exam"]["admit_card"]).status_code == 404
    Exam.objects.filter(id=exam.id).update(admit_cards_from=date.today())
    card = student.get("/api/v1" + body["exam"]["admit_card"])
    assert card.status_code == 200 and card.content[:4] == b"%PDF"


@pytest.mark.django_db
def test_student_id_card(student):
    body = student.get("/api/v1/student/me").json()
    assert body["id_card"]["qr"].startswith("EDUFLOW:GHIS:")
    assert body["id_card"]["principal"] == "S. Krishnan"


@pytest.mark.django_db
def test_homework_done_in_notebook_and_pdf(student, kabir):
    hw = Homework.objects.filter(class_group=kabir.class_group).order_by("-due_date").first()
    done = student.post(f"/api/v1/homework/{hw.id}/done", {"student_id": str(kabir.id)}, format="json")
    assert done.status_code == 201 and done.json()["submission"]["in_notebook"] is True
    pdf = SimpleUploadedFile("work.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    up = student.post(f"/api/v1/homework/{hw.id}/submissions", {"student_id": str(kabir.id), "photos": [pdf]}, format="multipart")
    assert up.status_code == 201
    fake = SimpleUploadedFile("work.pdf", b"not a pdf", content_type="application/pdf")
    assert student.post(f"/api/v1/homework/{hw.id}/submissions", {"student_id": str(kabir.id), "photos": [fake]}, format="multipart").status_code == 400
