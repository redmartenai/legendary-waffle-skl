"""RLS on every Phase 3 table, tested at the database layer with unfiltered queries (docs/security/rls.md)."""

import pytest
from django.db import ProgrammingError, connection, transaction

from eduflow.academics.models import AcademicYear, Campus, Department, Grade, Section, Subject
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.people.models import (
    Enrollment,
    Guardian,
    StaffProfile,
    Student,
    StudentGuardian,
    TeacherAssignment,
)

pytestmark = pytest.mark.django_db

MODELS = [
    Campus,
    AcademicYear,
    Department,
    Grade,
    Section,
    Subject,
    StaffProfile,
    Student,
    Guardian,
    StudentGuardian,
    Enrollment,
    TeacherAssignment,
]


def test_every_phase3_table_has_rls_enabled():
    tables = [m._meta.db_table for m in MODELS]
    with connection.cursor() as cursor:
        cursor.execute("SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relrowsecurity", [tables])
        enabled = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT DISTINCT tablename FROM pg_policies WHERE tablename = ANY(%s)", [tables])
        with_policy = {row[0] for row in cursor.fetchall()}
    assert enabled == with_policy == set(tables)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_missing_tenant_context_sees_nothing(world, other_world, model):
    assert model.objects.exists()
    with db_context.scoped(DbContext()):
        assert not model.objects.exists()


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_tenant_reads_only_its_school(world, other_world, model):
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert set(model.objects.values_list("school_id", flat=True)) == {world.school.pk}


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_tenant_cannot_update_or_delete_another_school(world, other_world, model):
    total_b = model.objects.filter(school=other_world.school).count()
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert model.objects.filter(school=other_world.school).update(school=other_world.school) == 0
        assert model.objects.filter(school=other_world.school).delete()[0] == 0
    assert model.objects.filter(school=other_world.school).count() == total_b


def test_tenant_cannot_insert_into_another_school(world, other_world):
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        for build in (
            lambda: Grade.objects.create(school=other_world.school, name="Injected", code="inj"),
            lambda: Student.objects.create(school=other_world.school, admission_number="X", first_name="X"),
            lambda: Guardian.objects.create(school=other_world.school, full_name="X"),
        ):
            with pytest.raises(ProgrammingError, match="row-level security"), transaction.atomic():
                build()


def test_requests_run_under_rls(world, as_member):
    """End to end: an admin request lists only its school (the view filters too; RLS backs it up)."""
    rows = as_member(world.admin).get("/api/v1/grades").json()["results"]
    assert {r["code"] for r in rows} == {"g5"}
