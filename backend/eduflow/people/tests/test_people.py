"""Staff, students, guardians, enrollments and teacher assignments: rules and integrity."""

import pytest
from django.db import IntegrityError, transaction

from eduflow.audit.models import AuditEvent
from eduflow.people.models import Enrollment, StaffProfile, Student, StudentGuardian, TeacherAssignment

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(world, as_member):
    return as_member(world.admin)


# ------------------------------------------------------------------------------------------------ staff
def test_create_teacher_profile_for_a_member(world, admin, make_member):
    member = make_member(world.school, roles=["teacher"])
    response = admin.post(
        "/api/v1/staff",
        {
            "membership_id": str(member.id),
            "employee_id": "T-9",
            "staff_type": "teaching",
            "department_id": str(world.department.id),
            "joining_date": "2026-06-01",
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["full_name"] == member.user.full_name
    assert body["department"]["id"] == str(world.department.id)
    assert "email" not in body
    assert "phone" not in body
    teachers = admin.get("/api/v1/staff?staff_type=teaching").json()["results"]
    assert {t["employee_id"] for t in teachers} == {"T-1", "T-9"}


def test_staff_needs_an_active_membership_of_this_school(world, other_world, admin, make_member):
    foreign = admin.post("/api/v1/staff", {"membership_id": str(other_world.admin.id), "employee_id": "X"})
    assert foreign.status_code == 400
    inactive = make_member(world.school, roles=[], is_active=False)
    assert (
        admin.post("/api/v1/staff", {"membership_id": str(inactive.id), "employee_id": "X"}).status_code
        == 400
    )
    taken = admin.post("/api/v1/staff", {"membership_id": str(world.teacher.id), "employee_id": "X"})
    assert taken.status_code == 400


def test_employee_id_is_unique_per_school(world, admin, make_member):
    member = make_member(world.school, roles=[])
    assert (
        admin.post("/api/v1/staff", {"membership_id": str(member.id), "employee_id": "T-1"}).status_code
        == 409
    )


def test_staff_leaving_ends_their_assignments(world, admin):
    response = admin.patch(f"/api/v1/staff/{world.teacher_staff.id}", {"status": "left"}, format="json")
    assert response.status_code == 200
    world.assignment.refresh_from_db()
    assert world.assignment.status == "ended"


def test_staff_membership_must_match_school_in_the_database(world, other_world):
    with pytest.raises(IntegrityError), transaction.atomic():
        StaffProfile.objects.create(school=world.school, membership=other_world.principal, employee_id="Z")


# ------------------------------------------------------------------------------------------------ students
def test_create_and_update_student(world, admin, make_member):
    login = make_member(world.school, roles=["student"])
    response = admin.post(
        "/api/v1/students",
        {
            "admission_number": "S-10",
            "first_name": "Meera",
            "last_name": "Iyer",
            "date_of_birth": "2015-04-01",
            "gender": "female",
            "membership_id": str(login.id),
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["full_name"] == "Meera Iyer"
    assert body["membership_id"] == str(login.id)
    assert not {"password", "user", "email", "phone"} & set(body)
    updated = admin.patch(f"/api/v1/students/{body['id']}", {"middle_name": "R"}, format="json")
    assert updated.json()["full_name"] == "Meera R Iyer"
    assert (
        admin.post("/api/v1/students", {"admission_number": "S-10", "first_name": "Dup"}).status_code == 409
    )


def test_student_filters_by_active_enrollment(world, admin):
    in_a = admin.get(f"/api/v1/students?section_id={world.section_a.id}").json()["results"]
    assert [s["id"] for s in in_a] == [str(world.student.id)]
    in_year = admin.get(f"/api/v1/students?academic_year_id={world.year.id}").json()["results"]
    assert len(in_year) == 2


# ------------------------------------------------------------------------------------------------ guardians
def test_multiple_guardians_and_multiple_children(world, admin):
    father = admin.post("/api/v1/guardians", {"full_name": "Father", "phone": "98111 11111"}).json()
    assert father["phone"] == "+919811111111"
    for student in (world.student, world.other_student):
        response = admin.post(
            "/api/v1/student-guardians",
            {"student_id": str(student.id), "guardian_id": father["id"], "relationship": "father"},
            format="json",
        )
        assert response.status_code == 201, response.content
    assert StudentGuardian.objects.filter(guardian_id=father["id"]).count() == 2
    guardians_of_student = admin.get(f"/api/v1/guardians?student_id={world.student.id}").json()["results"]
    assert {g["full_name"] for g in guardians_of_student} == {"Parent of Asha", "Father"}


def test_one_primary_guardian_per_student(world, admin):
    second = admin.post("/api/v1/guardians", {"full_name": "Second"}).json()
    response = admin.post(
        "/api/v1/student-guardians",
        {
            "student_id": str(world.student.id),
            "guardian_id": second["id"],
            "relationship": "father",
            "is_primary": True,
        },
        format="json",
    )
    assert response.status_code == 201
    world.link.refresh_from_db()
    assert world.link.is_primary is False


def test_duplicate_link_and_cross_school_link_are_rejected(world, other_world, admin):
    dup = admin.post(
        "/api/v1/student-guardians",
        {
            "student_id": str(world.student.id),
            "guardian_id": str(world.guardian.id),
            "relationship": "mother",
        },
    )
    assert dup.status_code == 409
    foreign = admin.post(
        "/api/v1/student-guardians",
        {
            "student_id": str(world.student.id),
            "guardian_id": str(other_world.guardian.id),
            "relationship": "mother",
        },
    )
    assert foreign.status_code == 400


def test_unlink_guardian(world, admin):
    assert admin.delete(f"/api/v1/student-guardians/{world.link.id}").status_code == 204
    event = AuditEvent.objects.get(action="people.guardian.unlinked")
    assert event.target_id == str(world.link.id)


def test_cross_school_link_is_refused_by_the_database(world, other_world):
    with pytest.raises(IntegrityError), transaction.atomic():
        StudentGuardian.objects.create(
            school=world.school, student=world.student, guardian=other_world.guardian, relationship="other"
        )


# ------------------------------------------------------------------------------------------------ enrollment
def _enroll(admin, student, section, **extra):
    return admin.post(
        "/api/v1/enrollments",
        {"student_id": str(student.id), "section_id": str(section.id), **extra},
        format="json",
    )


def test_valid_enrollment_takes_year_and_grade_from_the_section(world, admin):
    new = admin.post("/api/v1/students", {"admission_number": "S-3", "first_name": "Kiran"}).json()
    student = Student.objects.get(pk=new["id"])
    response = _enroll(admin, student, world.section_b, roll_number="7", start_date="2026-06-10")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["academic_year"]["id"] == str(world.year.id)
    assert body["grade"]["id"] == str(world.grade.id)
    assert body["status"] == "active"


def test_client_cannot_choose_year_or_grade(world, admin):
    response = _enroll(admin, world.other_student, world.section_a, academic_year_id=str(world.year.id))
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"academic_year_id": ["Unknown field."]}


def test_duplicate_active_enrollment_in_a_year_is_rejected(world, admin):
    assert _enroll(admin, world.student, world.section_b).status_code == 409


def test_cross_school_enrollment_is_rejected(world, other_world, admin):
    assert _enroll(admin, other_world.student, world.section_b).status_code == 400
    assert _enroll(admin, world.student, other_world.section_b).status_code == 400


def test_section_must_match_grade_and_year_in_the_database(world, other_world):
    from eduflow.academics.models import Grade

    other_grade = Grade.objects.create(school=world.school, name="Grade 6", code="g6")
    with pytest.raises(IntegrityError), transaction.atomic():
        Enrollment.objects.create(
            school=world.school,
            student=world.other_student,
            academic_year=world.year,
            grade=other_grade,  # section_a belongs to Grade 5
            section=world.section_a,
            start_date=world.year.start_date,
            status="withdrawn",
            end_date=world.year.start_date,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Enrollment.objects.create(
            school=world.school,
            student=world.other_student,
            academic_year=world.year,
            grade=world.grade,
            section=other_world.section_a,
            start_date=world.year.start_date,
            status="withdrawn",
            end_date=world.year.start_date,
        )


def test_enrollment_lifecycle_withdraw_is_final(world, admin):
    url = f"/api/v1/enrollments/{world.other_enrollment.id}"
    ended = admin.post(
        f"{url}/end", {"status": "withdrawn", "end_date": "2026-09-01", "reason": "Moved city"}
    )
    assert ended.status_code == 200, ended.content
    assert ended.json()["status"] == "withdrawn"
    assert ended.json()["end_date"] == "2026-09-01"
    assert admin.post(f"{url}/end", {"status": "completed"}).status_code == 409
    assert admin.patch(url, {"roll_number": "1"}, format="json").status_code == 409
    # Withdrawn, so the student can be enrolled again this year.
    assert _enroll(admin, world.other_student, world.section_a).status_code == 201


def test_end_date_before_start_is_rejected(world, admin):
    response = admin.post(
        f"/api/v1/enrollments/{world.enrollment.id}/end", {"status": "completed", "end_date": "2026-01-01"}
    )
    assert response.status_code == 400


def test_transfer_moves_the_student_and_links_records(world, admin):
    response = admin.post(
        f"/api/v1/enrollments/{world.enrollment.id}/transfer",
        {"section_id": str(world.section_b.id), "date": "2026-10-01", "reason": "Stream change"},
    )
    assert response.status_code == 200, response.content
    new = response.json()
    assert new["section"]["id"] == str(world.section_b.id)
    assert new["status"] == "active"
    world.enrollment.refresh_from_db()
    assert world.enrollment.status == "transferred"
    assert str(world.enrollment.transferred_to_id) == new["id"]
    assert AuditEvent.objects.filter(action="people.enrollment.transferred").exists()


def test_transfer_validations(world, admin):
    url = f"/api/v1/enrollments/{world.enrollment.id}/transfer"
    assert admin.post(url, {"section_id": str(world.section_a.id)}).status_code == 400  # same section
    from eduflow.academics.models import AcademicYear, Section

    next_year = AcademicYear.objects.create(
        school=world.school, name="2027-28", start_date="2027-06-01", end_date="2028-03-31", status="active"
    )
    other_year_section = Section.objects.create(
        school=world.school, academic_year=next_year, grade=world.grade, name="A", code="5a"
    )
    assert admin.post(url, {"section_id": str(other_year_section.id)}).status_code == 400


def test_roll_numbers_are_unique_among_active_enrollments_of_a_section(world, admin):
    admin.patch(f"/api/v1/enrollments/{world.enrollment.id}", {"roll_number": "1"}, format="json")
    new = admin.post("/api/v1/students", {"admission_number": "S-4", "first_name": "Dev"}).json()
    student = Student.objects.get(pk=new["id"])
    assert _enroll(admin, student, world.section_a, roll_number="1").status_code == 409


def test_inactive_student_or_archived_section_cannot_enroll(world, admin):
    Student.objects.filter(pk=world.other_student.pk).update(status="left")
    world.other_enrollment.delete()
    assert _enroll(admin, world.other_student, world.section_a).status_code == 400


# ------------------------------------------------------------------------------------------------ assignments
def test_assign_teacher_and_class_teacher(world, admin):
    second = admin.post(
        "/api/v1/teacher-assignments",
        {
            "staff_id": str(world.teacher_staff.id),
            "section_id": str(world.section_b.id),
            "is_class_teacher": True,
        },
        format="json",
    )
    assert second.status_code == 201, second.content
    assert second.json()["subject"] is None
    assert second.json()["academic_year"]["id"] == str(world.year.id)
    assert second.json()["staff"]["full_name"] == world.teacher.user.full_name


def test_one_class_teacher_per_section(world, admin, make_member):
    other = StaffProfile.objects.create(
        school=world.school, membership=make_member(world.school), employee_id="T-2"
    )
    body = {"section_id": str(world.section_a.id), "is_class_teacher": True}
    assert (
        admin.post(
            "/api/v1/teacher-assignments", {**body, "staff_id": str(world.teacher_staff.id)}, format="json"
        ).status_code
        == 201
    )
    assert (
        admin.post(
            "/api/v1/teacher-assignments", {**body, "staff_id": str(other.id)}, format="json"
        ).status_code
        == 409
    )


def test_assignment_validations(world, other_world, admin, make_member):
    base = {"staff_id": str(world.teacher_staff.id), "section_id": str(world.section_b.id)}
    assert admin.post("/api/v1/teacher-assignments", base, format="json").status_code == 400  # no subject
    cross = {**base, "subject_id": str(other_world.subject.id)}
    assert admin.post("/api/v1/teacher-assignments", cross, format="json").status_code == 400
    cross_section = {**base, "section_id": str(other_world.section_a.id), "subject_id": str(world.subject.id)}
    assert admin.post("/api/v1/teacher-assignments", cross_section, format="json").status_code == 400
    clerk = StaffProfile.objects.create(
        school=world.school,
        membership=make_member(world.school),
        employee_id="C-1",
        staff_type="non_teaching",
    )
    non_teacher = {
        "staff_id": str(clerk.id),
        "section_id": str(world.section_b.id),
        "subject_id": str(world.subject.id),
    }
    assert admin.post("/api/v1/teacher-assignments", non_teacher, format="json").status_code == 400
    duplicate = {
        "staff_id": str(world.teacher_staff.id),
        "section_id": str(world.section_a.id),
        "subject_id": str(world.subject.id),
    }
    assert admin.post("/api/v1/teacher-assignments", duplicate, format="json").status_code == 409


def test_assignment_year_must_match_section_in_the_database(world):
    from eduflow.academics.models import AcademicYear

    other_year = AcademicYear.objects.create(
        school=world.school, name="2027-28", start_date="2027-06-01", end_date="2028-03-31"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        TeacherAssignment.objects.create(
            school=world.school,
            staff=world.teacher_staff,
            academic_year=other_year,
            section=world.section_b,
            subject=world.subject,
        )


def test_end_and_delete_assignment(world, admin):
    mistake = admin.post(
        "/api/v1/teacher-assignments",
        {
            "staff_id": str(world.teacher_staff.id),
            "section_id": str(world.section_b.id),
            "is_class_teacher": True,
        },
        format="json",
    ).json()
    assert admin.delete(f"/api/v1/teacher-assignments/{mistake['id']}").status_code == 204  # a mistake
    url = f"/api/v1/teacher-assignments/{world.assignment.id}"
    assert admin.patch(url, {"status": "ended"}, format="json").json()["status"] == "ended"
    assert admin.patch(url, {"status": "active"}, format="json").status_code == 409
    assert admin.delete(url).status_code == 409  # ended assignments are teaching history


def test_people_changes_are_audited(world, admin):
    admin.post("/api/v1/students", {"admission_number": "S-5", "first_name": "Ana"})
    admin.post(f"/api/v1/enrollments/{world.other_enrollment.id}/end", {"status": "completed"})
    admin.patch(
        f"/api/v1/teacher-assignments/{world.assignment.id}", {"is_class_teacher": True}, format="json"
    )
    actions = set(AuditEvent.objects.filter(school_id=world.school.id).values_list("action", flat=True))
    assert {
        "people.student.created",
        "people.enrollment.completed",
        "people.teacher_assignment.updated",
    } <= actions
