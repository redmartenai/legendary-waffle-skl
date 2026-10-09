"""List endpoints run a bounded number of queries, whatever the page size (no N+1)."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from eduflow.people.models import Enrollment, Student, TeacherAssignment

pytestmark = pytest.mark.django_db

LISTS = [
    "/api/v1/sections",
    "/api/v1/subjects",
    "/api/v1/staff",
    "/api/v1/students",
    "/api/v1/student-guardians",
    "/api/v1/enrollments",
    "/api/v1/teacher-assignments",
]


def _grow(world, n):
    for i in range(n):
        student = Student.objects.create(school=world.school, admission_number=f"N-{i}", first_name=f"N{i}")
        Enrollment.objects.create(
            school=world.school,
            student=student,
            academic_year=world.year,
            grade=world.grade,
            section=world.section_b,
            start_date=datetime.date(2026, 6, 1),
        )
    TeacherAssignment.objects.create(
        school=world.school,
        staff=world.teacher_staff,
        academic_year=world.year,
        section=world.section_b,
        is_class_teacher=True,
    )


@pytest.mark.parametrize("path", LISTS)
def test_list_query_count_does_not_grow_with_rows(world, as_member, path):
    client = as_member(world.admin)
    client.get(path)  # warm the permission cache
    with CaptureQueriesContext(connection) as small:
        assert client.get(path).status_code == 200
    _grow(world, 15)
    with CaptureQueriesContext(connection) as large:
        assert client.get(path).status_code == 200
    assert len(large) == len(small), [q["sql"] for q in large.captured_queries]
