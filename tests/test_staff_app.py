"""Staff app: marks entry, assignment grading, leave, covers, documents, student profile, chat."""

from datetime import date, time, timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.academics.models import ClassGroup, Student, Subject, TimetableSlot
from apps.documents.models import Document
from apps.learning.models import Assignment, AssignmentSubmission
from apps.messaging.models import Conversation, ConversationMember, Meeting
from apps.results.models import Exam, ExamMark, MarkSheet
from apps.staff.models import StaffLeave, Substitution

from .conftest import PARENT_MEERA, PRINCIPAL, STUDENT_KABIR, TEACHER_ANITA, TEACHER_VIKRAM, api, user


@pytest.fixture
def anita(in_ghis):
    return api(TEACHER_ANITA)


@pytest.fixture
def group_10c(in_ghis):
    return ClassGroup.objects.get(grade="10", section="C")


@pytest.fixture
def sheet(group_10c):
    exam = Exam.objects.create(class_group=group_10c, name="UT3", held_on=date.today() - timedelta(days=2))
    return MarkSheet.objects.create(exam=exam, subject=Subject.objects.get(code="MATH"), max_marks=40, due_on=date.today() + timedelta(days=3))


@pytest.mark.django_db
def test_marks_autosave_validates_and_submit_needs_everyone(anita, sheet, group_10c):
    students = list(Student.objects.filter(class_group=group_10c, is_active=True).order_by("roll_no"))
    listed = anita.get("/api/v1/teacher/marks").json()["sheets"]
    assert [s["id"] for s in listed] == [str(sheet.id)]
    body = anita.put(
        f"/api/v1/marksheets/{sheet.id}",
        {"entries": [{"student_id": str(students[0].id), "marks": 34}, {"student_id": str(students[1].id), "marks": 43}, {"student_id": str(students[2].id), "absent": True}]},
        format="json",
    ).json()
    assert body["errors"] == {str(students[1].id): "Max is 40 — recheck the answer sheet."}
    assert body["entered"] == 2 and body["students"][2]["absent"] is True
    assert anita.post(f"/api/v1/marksheets/{sheet.id}/submit").status_code == 400
    entries = [{"student_id": str(s.id), "marks": 30} for s in students[1:2] + students[3:]]
    anita.put(f"/api/v1/marksheets/{sheet.id}", {"entries": entries}, format="json")
    done = anita.post(f"/api/v1/marksheets/{sheet.id}/submit")
    assert done.status_code == 200 and done.json()["status"] == "submitted"
    # Submitted marks are locked for the teacher.
    assert anita.put(f"/api/v1/marksheets/{sheet.id}", {"entries": entries}, format="json").status_code == 403


@pytest.mark.django_db
def test_marks_only_for_the_subject_teacher(sheet):
    # Vikram teaches English in 10C, not Maths.
    assert api(TEACHER_VIKRAM).get(f"/api/v1/marksheets/{sheet.id}").status_code == 404


@pytest.mark.django_db
def test_absent_marks_are_left_out_of_results(in_ghis, group_10c):
    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    exam = Exam.objects.create(class_group=group_10c, name="Absent test", held_on=date.today(), is_published=True)
    maths, sci = Subject.objects.get(code="MATH"), Subject.objects.get(code="SCI")
    ExamMark.objects.create(exam=exam, student=kabir, subject=maths, marks=0, max_marks=100, is_absent=True)
    ExamMark.objects.create(exam=exam, student=kabir, subject=sci, marks=80, max_marks=100)
    body = api(STUDENT_KABIR).get(f"/api/v1/students/{kabir.id}/results").json()
    row = next(e for e in body["exams"] if e["name"] == "Absent test")
    assert row["percent"] == 80.0 and [s["subject"] for s in row["subjects"]] == ["Science"]


@pytest.mark.django_db
def test_grade_assignment_against_rubric(anita, group_10c):
    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    a = Assignment.objects.create(
        class_group=group_10c, subject=Subject.objects.get(code="MATH"), title="Poster", max_marks=20, due_date=date.today() + timedelta(days=5),
        rubric=[{"key": "concept", "label": "Concept", "max": 10}, {"key": "style", "label": "Style", "max": 10}], created_by=user(TEACHER_ANITA),
    )
    sub = AssignmentSubmission.objects.create(assignment=a, student=kabir, submitted_at=timezone.now())
    review = anita.get(f"/api/v1/assignments/{a.id}/review").json()
    assert review["assignment"]["counts"]["to_review"] == 1 and review["to_review"][0]["student"]["first_name"] == "Kabir"
    bad = anita.post(f"/api/v1/assignments/submissions/{sub.id}/grade", {"scores": {"concept": 11, "style": 8}}, format="json")
    assert bad.status_code == 400
    ok = anita.post(f"/api/v1/assignments/submissions/{sub.id}/grade", {"scores": {"concept": 8, "style": 8}, "feedback": "Good"}, format="json").json()
    assert ok["total"] == 16 and ok["status"] == "graded" and ok["grade"]
    assert api(TEACHER_VIKRAM).get(f"/api/v1/assignments/{a.id}/review").status_code == 404


@pytest.mark.django_db
def test_leave_request_balance_and_rules(anita):
    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    body = anita.get("/api/v1/staff/leave").json()
    casual = next(b for b in body["balances"] if b["kind"] == "casual")
    assert casual["left"] == 12
    made = anita.post("/api/v1/staff/leave", {"kind": "casual", "from_date": monday.isoformat(), "to_date": (monday + timedelta(days=1)).isoformat(), "reason": "Wedding"}, format="json")
    assert made.status_code == 201 and made.json()["days"] == 2
    # Overlapping and over-long sick leave are refused.
    assert anita.post("/api/v1/staff/leave", {"kind": "casual", "from_date": monday.isoformat(), "reason": "Again"}, format="json").status_code == 400
    far = monday + timedelta(days=14)
    sick = anita.post("/api/v1/staff/leave", {"kind": "sick", "from_date": far.isoformat(), "to_date": (far + timedelta(days=3)).isoformat(), "reason": "Flu"}, format="json")
    assert sick.status_code == 400 and "certificate" in sick.json()["error"]["fields"]
    cancel = anita.post(f"/api/v1/staff/leave/{made.json()['id']}/cancel")
    assert cancel.json()["status"] == "cancelled"
    assert StaffLeave.objects.filter(status="pending").count() == 0


@pytest.mark.django_db
def test_timetable_shows_cover_and_handover(anita, group_10c):
    today = date.today()
    if today.weekday() == 6:
        pytest.skip("No school on Sunday")
    vikram_slot = TimetableSlot.objects.filter(teacher=user(TEACHER_VIKRAM), weekday=today.weekday()).exclude(
        period__in=TimetableSlot.objects.filter(teacher=user(TEACHER_ANITA), weekday=today.weekday()).values_list("period", flat=True)
    ).first()
    if vikram_slot is None:
        pytest.skip("No free period to cover today")
    cover = Substitution.objects.create(date=today, slot=vikram_slot, teacher=user(TEACHER_ANITA), absent_teacher=user(TEACHER_VIKRAM), reason="on leave", assigned_by=user(PRINCIPAL), assigned_at=timezone.now())
    day = anita.get("/api/v1/staff/timetable").json()["day"]
    cell = next(c for c in day["cells"] if c.get("period") == vikram_slot.period)
    assert cell["kind"] == "cover" and cell["cover"]["for"] == "Vikram Das"
    note = anita.post(f"/api/v1/staff/covers/{cover.id}/note", {"note": "Finished ch 3"}, format="json")
    assert note.status_code == 200 and note.json()["note"] == "Finished ch 3"
    # Only the covering teacher can leave the note.
    assert api(TEACHER_VIKRAM).post(f"/api/v1/staff/covers/{cover.id}/note", {"note": "x"}, format="json").status_code == 404


@pytest.mark.django_db
def test_private_documents_and_exam_cell_lock(anita, in_ghis):
    upload = SimpleUploadedFile("plan.pdf", b"%PDF-1.4 plan", content_type="application/pdf")
    made = anita.post("/api/v1/staff/documents", {"kind": "certificate", "title": "B.Ed.", "file": upload}, format="multipart").json()
    assert made["status"] == "in_review"
    # Another teacher can't open Anita's private certificate.
    assert api(TEACHER_VIKRAM).get(f"/api/v1{made['download']}").status_code == 404
    assert anita.get(f"/api/v1{made['download']}").status_code == 200
    paper = Document.objects.create(
        kind="question_paper", title="Set B", file=ContentFile(b"%PDF", name="b.pdf"), audience=Document.Audience.PRIVATE,
        owner=user(TEACHER_ANITA), locked_until=date.today() + timedelta(days=10),
    )
    assert anita.get(f"/api/v1/documents/{paper.id}/file").status_code == 404
    mine = anita.get("/api/v1/staff/documents").json()
    locked = next(i for s in mine["sections"] if s["key"] == "question_paper" for i in s["items"])
    assert locked["locked"] is True and locked["download"] is None


@pytest.mark.django_db
def test_student_profile_for_their_teachers_only(anita, in_ghis):
    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    body = anita.get(f"/api/v1/staff/students/{kabir.id}").json()
    assert body["student"]["name"] == kabir.full_name and "•" in body["guardian"]["phone"]
    other = Student.objects.filter(class_group__grade="6").first()
    assert anita.get(f"/api/v1/staff/students/{other.id}").status_code == 404
    assert api(PARENT_MEERA).get(f"/api/v1/staff/students/{kabir.id}").status_code == 403


@pytest.mark.django_db
def test_colleague_chat_and_ptm_request(anita, in_ghis):
    contacts = anita.get("/api/v1/chat/contacts").json()["contacts"]
    vikram = next(c for c in contacts if c["name"] == "Vikram Das")
    started = anita.post("/api/v1/chat/conversations", {"kind": "colleague", "user_id": vikram["user_id"]}, format="json")
    assert started.status_code == 201 and started.json()["counterpart"] == "colleague"
    # Parents can't use the colleague route.
    assert api(PARENT_MEERA).post("/api/v1/chat/conversations", {"kind": "colleague", "user_id": vikram["user_id"]}, format="json").status_code == 403

    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    chat = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=kabir)
    ConversationMember.objects.create(conversation=chat, user=user(PARENT_MEERA), side="family")
    ConversationMember.objects.create(conversation=chat, user=user(TEACHER_ANITA), side="staff")
    starts = timezone.now() + timedelta(days=3)
    meeting = Meeting.objects.create(conversation=chat, title="PTM", starts_at=starts, ends_at=starts + timedelta(minutes=15), status="requested")
    listed = next(c for c in anita.get("/api/v1/chat/conversations").json()["conversations"] if c["id"] == str(chat.id))
    assert listed["pending_meeting"]["id"] == str(meeting.id) and listed["counterpart"] == "parent"
    accepted = anita.post(f"/api/v1/meetings/{meeting.id}/accept")
    assert accepted.status_code == 200 and accepted.json()["status"] == "booked"
    assert anita.post(f"/api/v1/meetings/{meeting.id}/accept").status_code == 400


@pytest.mark.django_db
def test_staff_home_and_classes(anita):
    home = anita.get("/api/v1/staff/home")
    assert home.status_code == 200 and home.json()["register"]["class"]["short_label"] == "8-B"
    classes = anita.get("/api/v1/staff/classes").json()["classes"]
    assert {c["short_label"] for c in classes} >= {"8-B", "10-C"}
    assert api(PARENT_MEERA).get("/api/v1/staff/home").status_code == 403
    _ = time
