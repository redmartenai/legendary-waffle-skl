"""Console academics area: the academics page (allocation, homework load), the timetable (draft, clashes, publish,
cover) and examinations (marks matrix, checklist, publishing, moderation, invigilators)."""

from datetime import date, time, timedelta

import pytest

from apps.academics.models import ClassGroup, Student, Subject, TeachingAssignment, TimetableDraft, TimetableSlot
from apps.accounts.models import AuditLog
from apps.approvals.models import ApprovalRequest
from apps.approvals.services import open_request
from apps.homework.models import Homework
from apps.results.models import Exam, ExamMark, ExamPaper, ExamSeries, Invigilation, MarkCorrection, MarkSheet
from apps.staff.models import StaffLeave, Substitution

from .conftest import PARENT_MEERA, PRINCIPAL, STUDENT_KABIR, TEACHER_ANITA, TEACHER_VIKRAM, api, user


@pytest.fixture
def principal(in_ghis):
    return api(PRINCIPAL)


@pytest.fixture
def g10c(in_ghis):
    return ClassGroup.objects.get(grade="10", section="C")


def _free_period(teacher, weekday=0):
    busy = set(TimetableSlot.objects.filter(teacher=teacher, weekday=weekday).values_list("period", flat=True))
    return next(p for p in range(1, 9) if p not in busy)


# ------------------------------------------------------------------ roles


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/api/v1/console/academics", "/api/v1/console/timetable", "/api/v1/console/timetable/cover", "/api/v1/console/exams"])
def test_teachers_and_parents_are_refused(in_ghis, path):
    assert api(TEACHER_ANITA).get(path).status_code == 403
    assert api(PARENT_MEERA).get(path).status_code == 403


@pytest.mark.django_db
def test_mutations_are_refused_for_teachers(in_ghis, g10c):
    anita = api(TEACHER_ANITA)
    assert anita.post("/api/v1/console/academics/allocate", {}, format="json").status_code == 403
    assert anita.post("/api/v1/console/timetable/publish").status_code == 403
    assert anita.post("/api/v1/console/exams/publish", {"series": "Term 1"}, format="json").status_code == 403
    assert anita.patch("/api/v1/console/academics/policy", {"homework_limit": 2}, format="json").status_code == 403


# ------------------------------------------------------------------ academics


@pytest.mark.django_db
def test_academics_page_and_filling_a_gap(principal, g10c):
    maths = Subject.objects.get(code="MATH")
    TeachingAssignment.objects.filter(class_group=g10c, subject=maths).delete()
    TimetableSlot.objects.filter(class_group=g10c, subject=maths).update(teacher=None)
    body = principal.get("/api/v1/console/academics").json()
    assert body["structure"]["sections"] == 3
    gap = next(g for g in body["allocation"]["gaps"] if g["subject"]["code"] == "MATH")
    assert gap["sections"][0]["label"] == "10-C" and body["allocation"]["unassigned"] >= 1

    slots = list(TimetableSlot.objects.filter(class_group=g10c, subject=maths))
    anita = user(TEACHER_ANITA)
    # A teacher who is busy at one of those periods can't take them.
    clash_slot = slots[0]
    TimetableSlot.objects.filter(class_group__grade="8", weekday=clash_slot.weekday, period=clash_slot.period).update(teacher=anita)
    refused = principal.post("/api/v1/console/academics/allocate", {"subject_id": str(maths.id), "class_group_ids": [str(g10c.id)], "teacher_id": str(anita.id)}, format="json")
    assert refused.status_code == 400 and "teacher_id" in refused.json()["error"]["fields"]
    # Someone new with no periods is free.
    from apps.accounts.models import Membership, Role, User

    newbie = User.objects.create_user("+919800000077", "New Teacher")
    Membership.objects.create(user=newbie, school=g10c.school, role=Role.TEACHER)
    made = principal.post("/api/v1/console/academics/allocate", {"subject_id": str(maths.id), "class_group_ids": [str(g10c.id)], "teacher_id": str(newbie.id)}, format="json")
    assert made.status_code == 201
    assert TeachingAssignment.objects.filter(class_group=g10c, subject=maths, teacher=newbie).exists()
    assert set(TimetableSlot.objects.filter(class_group=g10c, subject=maths).values_list("teacher_id", flat=True)) == {newbie.id}
    assert AuditLog.objects.filter(action="academics.allocate").exists()
    # The same gap can't be filled twice.
    again = principal.post("/api/v1/console/academics/allocate", {"subject_id": str(maths.id), "class_group_ids": [str(g10c.id)], "teacher_id": str(newbie.id)}, format="json")
    assert again.status_code == 400


@pytest.mark.django_db
def test_homework_load_and_reminder(principal, g10c):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    maths = Subject.objects.get(code="MATH")
    anita = user(TEACHER_ANITA)
    # Nothing over the limit: the reminder has nobody to go to.
    assert principal.post("/api/v1/console/academics/homework/notify", {}, format="json").status_code == 400
    for i in range(4):
        Homework.objects.create(class_group=g10c, subject=maths, title=f"Set {i}", assigned_by=anita, assigned_on=monday, due_date=monday + timedelta(days=2))
    assert principal.patch("/api/v1/console/academics/policy", {"homework_limit": "x"}, format="json").status_code == 400
    assert principal.patch("/api/v1/console/academics/policy", {"homework_limit": 3}, format="json").json()["homework_limit"] == 3
    week = principal.get("/api/v1/console/academics").json()["homework"]
    row = next(r for r in week["rows"] if r["grade"] == "10")
    assert row["per_section"] >= 4 and row["over"] and week["over"] == ["10"]
    sent = principal.post("/api/v1/console/academics/homework/notify", {}, format="json")
    assert sent.status_code == 200 and sent.json()["sent_to"] >= 1


# ------------------------------------------------------------------ timetable


@pytest.mark.django_db
def test_timetable_draft_clash_and_publish(principal, g10c):
    g8b = ClassGroup.objects.get(grade="8", section="B")
    TimetableSlot.objects.all().delete()
    maths, eng, sci = (Subject.objects.get(code=c) for c in ("MATH", "ENG", "SCI"))
    anita, vikram = user(TEACHER_ANITA), user(TEACHER_VIKRAM)
    for g, period, subject, teacher, room in (
        (g10c, 1, maths, anita, "Room 305"),
        (g10c, 2, eng, vikram, "Room 305"),
        (g8b, 2, maths, anita, "Room 214"),
        (g8b, 1, sci, vikram, "Room 214"),
    ):
        TimetableSlot.objects.create(class_group=g, weekday=0, period=period, starts_at=time(8 + period), ends_at=time(8 + period, 45), subject=subject, teacher=teacher, room=room)

    view = principal.get("/api/v1/console/timetable", {"view": "class", "id": str(g10c.id)}).json()
    assert view["target"]["label"] == "10-C" and len(view["cells"]) == 2 and view["draft"]["periods"] == 0
    # Swapping 10-C's P1 and P2 puts Anita in 10-C and 8-B at P2 (and Vikram in both at P1).
    made = principal.post("/api/v1/console/timetable/draft/swap", {"class_id": str(g10c.id), "weekday": 0, "period": 1, "to_weekday": 0, "to_period": 2}, format="json")
    assert made.status_code == 201
    draft = made.json()
    assert draft["periods"] == 2 and len(draft["clashes"]) == 2 and draft["publishable"] == 0
    assert {c["with"]["class"] for c in draft["clashes"]} == {"8-B"}
    # The live timetable hasn't changed.
    assert TimetableSlot.objects.get(class_group=g10c, weekday=0, period=1).subject == maths
    # Publishing refuses when every change clashes.
    assert principal.post("/api/v1/console/timetable/publish").status_code == 400
    # A clash-free change in another class publishes; the clashing swap is held back.
    ok = principal.post("/api/v1/console/timetable/draft", {"class_id": str(g8b.id), "weekday": 0, "period": 1, "subject_id": str(eng.id), "teacher_id": str(user("+919800000004").id)}, format="json")
    assert ok.status_code == 201
    done = principal.post("/api/v1/console/timetable/publish")
    assert done.status_code == 200 and done.json()["applied"] == 1 and done.json()["held"] == 1
    assert TimetableSlot.objects.get(class_group=g8b, weekday=0, period=1).subject == eng
    assert TimetableDraft.objects.filter(class_group=g10c).count() == 2
    assert AuditLog.objects.filter(action="timetable.publish").exists()
    # Undo the swap.
    batch = TimetableDraft.objects.filter(class_group=g10c).first().batch
    assert principal.delete(f"/api/v1/console/timetable/draft/{batch}").json()["periods"] == 0
    # A period that doesn't exist is refused.
    bad = principal.post("/api/v1/console/timetable/draft", {"class_id": str(g10c.id), "weekday": 0, "period": 9, "subject_id": str(eng.id)}, format="json")
    assert bad.status_code == 400


@pytest.mark.django_db
def test_cover_suggestions_and_assign(principal):
    today = date.today()
    if today.weekday() == 6:
        pytest.skip("No school on Sunday")
    vikram = user(TEACHER_VIKRAM)
    StaffLeave.objects.create(user=vikram, kind="sick", from_date=today, to_date=today, days=1, reason="Fever", status="approved")
    board = principal.get("/api/v1/console/timetable/cover").json()
    assert board["on_leave"][0]["name"] == "Vikram Das"
    slot = TimetableSlot.objects.filter(teacher=vikram, weekday=today.weekday()).first()
    if slot is None:
        pytest.skip("Vikram teaches nothing today")
    anita = user(TEACHER_ANITA)
    TimetableSlot.objects.filter(teacher=anita, weekday=today.weekday(), period=slot.period).delete()
    assert principal.post("/api/v1/console/timetable/cover", {"slot_id": str(slot.id), "teacher_id": str(vikram.id)}, format="json").status_code == 400
    made = principal.post("/api/v1/console/timetable/cover", {"slot_id": str(slot.id), "teacher_id": str(anita.id)}, format="json")
    assert made.status_code == 201 and Substitution.objects.filter(slot=slot, teacher=anita).exists()
    assert principal.post("/api/v1/console/timetable/cover", {"slot_id": str(slot.id), "teacher_id": str(anita.id)}, format="json").status_code == 400


# ------------------------------------------------------------------ examinations


def _ut(g10c, *, locked=True):
    exam = Exam.objects.create(class_group=g10c, name="UT Console", held_on=date.today() - timedelta(days=3))
    maths, sci = Subject.objects.get(code="MATH"), Subject.objects.get(code="SCI")
    done = MarkSheet.objects.create(exam=exam, subject=maths, status=MarkSheet.Status.SUBMITTED)
    held = MarkSheet.objects.create(exam=exam, subject=sci, status=MarkSheet.Status.REVIEW, review_note="Outliers")
    for s in Student.objects.filter(class_group=g10c, is_active=True):
        ExamMark.objects.create(exam=exam, student=s, subject=maths, marks=70, max_marks=100)
        ExamMark.objects.create(exam=exam, student=s, subject=sci, marks=60, max_marks=100)
    if locked:
        ExamSeries.objects.create(name="UT Console", weightage=10)
    return exam, done, held


@pytest.mark.django_db
def test_exam_matrix_checklist_and_publish(principal, g10c):
    exam, done, held = _ut(g10c)
    body = principal.get("/api/v1/console/exams").json()
    assert body["series"] == "UT Console"
    row = body["matrix"]["rows"][0]
    states = {c["code"]: c["state"] for c in row["cells"]}
    assert states == {"MATH": "submitted", "SCI": "review"}
    check = body["checklist"]
    assert set(check["failures"]) == {"grading", "template"} and not check["can_publish"]
    # Blocked while the grading scale isn't locked and the template isn't approved.
    blocked = principal.post("/api/v1/console/exams/publish", {"series": "UT Console"}, format="json")
    assert blocked.status_code == 400
    principal.post("/api/v1/console/exams/series", {"series": "UT Console", "lock_grading": True}, format="json")
    principal.post("/api/v1/console/exams/series", {"series": "UT Console", "approve_template": True}, format="json")
    assert principal.get("/api/v1/console/exams").json()["checklist"]["can_publish"]
    made = principal.post("/api/v1/console/exams/publish", {"series": "UT Console"}, format="json")
    assert made.status_code == 200 and made.json()["published"] == 1
    done.refresh_from_db()
    held.refresh_from_db()
    assert done.status == MarkSheet.Status.PUBLISHED and held.status == MarkSheet.Status.REVIEW
    assert AuditLog.objects.filter(action="exams.publish").exists()
    # Families see the published subject only.
    kabir = Student.objects.get(user=user(STUDENT_KABIR))
    results = api(STUDENT_KABIR).get(f"/api/v1/students/{kabir.id}/results").json()
    row = next(e for e in results["exams"] if e["name"] == "UT Console")
    assert [s["subject"] for s in row["subjects"]] == ["Mathematics"]
    # Clearing moderation and publishing again releases the rest and marks the exam published.
    assert principal.post(f"/api/v1/console/exams/sheets/{held.id}/moderate").status_code == 200
    principal.post("/api/v1/console/exams/publish", {"series": "UT Console"}, format="json")
    exam.refresh_from_db()
    assert exam.is_published
    # Nothing left to publish.
    assert principal.post("/api/v1/console/exams/publish", {"series": "UT Console"}, format="json").status_code == 400


@pytest.mark.django_db
def test_moderation_entries_go_through_approvals(principal, g10c):
    exam, done, _held = _ut(g10c)
    done.status = MarkSheet.Status.PUBLISHED
    done.save()
    students = list(Student.objects.filter(class_group=g10c).order_by("roll_no")[:2])
    corr = MarkCorrection.objects.create(
        exam=exam, subject=done.subject, reason="Re-totalled",
        entries=[{"student_id": str(students[0].id), "from": 70, "to": 76, "note": "Q7"}, {"student_id": str(students[1].id), "from": 70, "to": 62, "note": "Q3 twice"}],
    )
    req = open_request(kind="marks", target=corr, requested_by=user(TEACHER_ANITA), summary="UT", notify_principal=False)
    queue = principal.get("/api/v1/console/exams").json()["moderation"]
    assert queue["corrections"][0]["entries"][1]["to"] == 62
    assert principal.post(f"/api/v1/console/exams/corrections/{corr.id}/entries/0", {"decision": "maybe"}, format="json").status_code == 400
    principal.post(f"/api/v1/console/exams/corrections/{corr.id}/entries/0", {"decision": "accept"}, format="json")
    req.refresh_from_db()
    assert req.status == ApprovalRequest.Status.PENDING
    principal.post(f"/api/v1/console/exams/corrections/{corr.id}/entries/1", {"decision": "reject"}, format="json")
    req.refresh_from_db()
    assert req.status == ApprovalRequest.Status.APPROVED
    assert ExamMark.objects.get(exam=exam, student=students[0], subject=done.subject).marks == 76
    assert ExamMark.objects.get(exam=exam, student=students[1], subject=done.subject).marks == 70


@pytest.mark.django_db
def test_invigilators_and_datesheet(principal, g10c):
    exam = Exam.objects.create(class_group=g10c, name="Half-yearly X", held_on=date.today() + timedelta(days=10))
    ExamSeries.objects.create(name="Half-yearly X", invigilators_per_room=2)
    paper = ExamPaper.objects.create(exam=exam, subject=Subject.objects.get(code="MATH"), date=date.today() + timedelta(days=10), starts_at=time(9), ends_at=time(12), room="Room 305")
    plan = principal.get("/api/v1/console/exams").json()["upcoming"]
    assert plan["name"] == "Half-yearly X" and plan["rows"][0]["needed"] == 2 and plan["rows"][0]["status"] == "none"
    made = principal.post("/api/v1/console/exams/invigilators").json()
    assert made["added"] == 2 and Invigilation.objects.filter(paper=paper).count() == 2
    assert made["upcoming"]["rows"][0]["status"] == "complete"
    sheet = principal.get("/api/v1/console/exams/datesheet")
    assert sheet.status_code == 200 and b"10-C" in sheet.content and b"Room 305" in sheet.content
    assert AuditLog.objects.filter(action="exams.datesheet_export").exists()
