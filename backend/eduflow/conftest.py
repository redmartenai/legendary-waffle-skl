"""Phase 3 test fixtures: a complete small school, built directly in the database."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from eduflow.academics.models import AcademicYear, Campus, Department, Grade, Section, Subject
from eduflow.people.models import (
    Enrollment,
    Guardian,
    StaffProfile,
    Student,
    StudentGuardian,
    TeacherAssignment,
)


@dataclass
class SchoolWorld:
    school: Any
    year: AcademicYear
    grade: Grade
    section_a: Section
    section_b: Section
    subject: Subject
    department: Department
    campus: Campus
    admin: Any
    principal: Any
    teacher: Any  # membership
    teacher_staff: StaffProfile
    assignment: TeacherAssignment
    student_member: Any
    student: Student  # in section A, signs in
    other_student: Student  # in section B, no login
    enrollment: Enrollment
    other_enrollment: Enrollment
    parent: Any
    guardian: Guardian
    link: StudentGuardian


@pytest.fixture
def build_world(make_school: Any, make_member: Any) -> Callable[[str], SchoolWorld]:
    def _build(code: str) -> SchoolWorld:
        school = make_school(code)
        year = AcademicYear.objects.create(
            school=school,
            name="2026-27",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 3, 31),
            status="active",
            is_current=True,
        )
        campus = Campus.objects.create(school=school, name="Main", code="main")
        department = Department.objects.create(school=school, name="Science", code="science")
        grade = Grade.objects.create(school=school, name="Grade 5", code="g5", display_order=5)
        section_a = Section.objects.create(
            school=school, academic_year=year, grade=grade, name="A", code="5a", campus=campus, capacity=40
        )
        section_b = Section.objects.create(
            school=school, academic_year=year, grade=grade, name="B", code="5b"
        )
        subject = Subject.objects.create(
            school=school, name="Mathematics", code="maths", department=department
        )

        admin = make_member(school, roles=["school_admin"])
        principal = make_member(school, roles=["principal"])
        teacher = make_member(school, roles=["teacher"])
        teacher_staff = StaffProfile.objects.create(
            school=school, membership=teacher, employee_id="T-1", department=department, campus=campus
        )
        assignment = TeacherAssignment.objects.create(
            school=school, staff=teacher_staff, academic_year=year, section=section_a, subject=subject
        )
        student_member = make_member(school, roles=["student"])
        student = Student.objects.create(
            school=school, membership=student_member, admission_number="S-1", first_name="Asha"
        )
        other_student = Student.objects.create(school=school, admission_number="S-2", first_name="Ravi")
        start = datetime.date(2026, 6, 1)
        enrollment = Enrollment.objects.create(
            school=school,
            student=student,
            academic_year=year,
            grade=grade,
            section=section_a,
            start_date=start,
        )
        other_enrollment = Enrollment.objects.create(
            school=school,
            student=other_student,
            academic_year=year,
            grade=grade,
            section=section_b,
            start_date=start,
        )
        parent = make_member(school, roles=["parent"])
        guardian = Guardian.objects.create(school=school, membership=parent, full_name="Parent of Asha")
        link = StudentGuardian.objects.create(
            school=school, student=student, guardian=guardian, relationship="mother", is_primary=True
        )
        return SchoolWorld(
            school=school,
            year=year,
            grade=grade,
            section_a=section_a,
            section_b=section_b,
            subject=subject,
            department=department,
            campus=campus,
            admin=admin,
            principal=principal,
            teacher=teacher,
            teacher_staff=teacher_staff,
            assignment=assignment,
            student_member=student_member,
            student=student,
            other_student=other_student,
            enrollment=enrollment,
            other_enrollment=other_enrollment,
            parent=parent,
            guardian=guardian,
            link=link,
        )

    return _build


@pytest.fixture
def world(build_world: Callable[[str], SchoolWorld]) -> SchoolWorld:
    return build_world("phase3-a")


@pytest.fixture
def other_world(build_world: Callable[[str], SchoolWorld]) -> SchoolWorld:
    return build_world("phase3-b")


@pytest.fixture
def as_member(client_for: Any) -> Any:
    """``as_member(membership)`` -> an API client signed in as that member, acting in their school."""

    def _client(membership: Any) -> Any:
        return client_for(membership.user, membership.school)

    return _client
