"""Academic structure: campuses, academic years, departments, grades, sections and subjects.

Every table is school-owned (``TenantModel``), protected by RLS (academics migration 0002), and carries a
``UNIQUE (id, school_id)`` target so child rows can use composite foreign keys that keep the school consistent
in the database itself (docs/architecture/phase-3.md).

Hierarchy (ADR-022)::

    School ── Grade (academic level, stable across years, e.g. "Grade 5")
          └── AcademicYear ── Section (Grade x Year, e.g. "5-A 2026-27")
"""

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q
from django.db.models.functions import Lower

from eduflow.core.ids import uuid7
from eduflow.tenancy.models import Address, TenantModel, TenantQuerySet


class RecordStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


def _same_school_target(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"academics_{model}_id_school_uniq")


def _unique_name(model: str, *scope: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(*scope, Lower("name"), name=f"academics_{model}_name_uniq")


class Campus(TenantModel, Address):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=32)
    status = models.CharField(max_length=16, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "academics_campus"
        constraints = [
            models.UniqueConstraint(fields=["school", "code"], name="academics_campus_code_uniq"),
            _unique_name("campus", "school"),
            _same_school_target("campus"),
        ]

    def __str__(self) -> str:
        return self.code


class AcademicYearStatus(models.TextChoices):
    PLANNED = "planned", "Planned"
    ACTIVE = "active", "Active"
    CLOSED = "closed", "Closed"


class DateRange(Func):
    function = "DATERANGE"
    output_field = DateRangeField()


class AcademicYear(TenantModel):
    """A school year. Lifecycle: planned -> active -> closed. Closed years are read-only history.

    Years of one school never overlap (exclusion constraint), and at most one is ``is_current``.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=50, help_text='For example "2026-27".')
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=16, choices=AcademicYearStatus.choices, default=AcademicYearStatus.PLANNED
    )
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "academics_academic_year"
        constraints = [
            models.CheckConstraint(
                condition=Q(start_date__lt=F("end_date")), name="academics_academic_year_dates_check"
            ),
            models.CheckConstraint(
                condition=Q(is_current=False) | Q(status=AcademicYearStatus.ACTIVE),
                name="academics_academic_year_current_is_active_check",
            ),
            models.UniqueConstraint(
                fields=["school"],
                condition=Q(is_current=True),
                name="academics_academic_year_one_current_uniq",
            ),
            _unique_name("academic_year", "school"),
            ExclusionConstraint(
                name="academics_academic_year_no_overlap",
                expressions=[
                    ("school", RangeOperators.EQUAL),
                    (DateRange("start_date", "end_date", models.Value("[]")), RangeOperators.OVERLAPS),
                ],
            ),
            _same_school_target("academic_year"),
        ]
        indexes = [models.Index(fields=["school", "-start_date"], name="academics_year_start_idx")]

    def __str__(self) -> str:
        return self.name


class Department(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=32)
    description = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=16, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "academics_department"
        constraints = [
            models.UniqueConstraint(fields=["school", "code"], name="academics_department_code_uniq"),
            _unique_name("department", "school"),
            _same_school_target("department"),
        ]

    def __str__(self) -> str:
        return self.code


class Grade(TenantModel):
    """An academic level ("Grade 5", "Class X", "KG-1"). Names are the school's own; nothing is hard-coded.

    The API calls these ``grades``: the client's ``/classes/{id}`` already means a section (ADR-007).
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=50)
    code = models.SlugField(max_length=32)
    display_order = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=16, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "academics_grade"
        constraints = [
            models.UniqueConstraint(fields=["school", "code"], name="academics_grade_code_uniq"),
            _unique_name("grade", "school"),
            _same_school_target("grade"),
        ]
        indexes = [models.Index(fields=["school", "display_order"], name="academics_grade_order_idx")]

    def __str__(self) -> str:
        return self.code


class Section(TenantModel):
    """A grade's division within one academic year (the client's "class", e.g. "Grade 9 · B")."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="sections")
    grade = models.ForeignKey(Grade, on_delete=models.PROTECT, related_name="sections")
    campus = models.ForeignKey(
        Campus, on_delete=models.PROTECT, null=True, blank=True, related_name="sections"
    )
    name = models.CharField(max_length=50, help_text='For example "A" or "Rose".')
    code = models.SlugField(max_length=32)
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "academics_section"
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "grade", "code"], name="academics_section_year_grade_code_uniq"
            ),
            models.UniqueConstraint(
                "academic_year", "grade", Lower("name"), name="academics_section_year_grade_name_uniq"
            ),
            models.CheckConstraint(
                condition=Q(capacity__isnull=True) | Q(capacity__gt=0),
                name="academics_section_capacity_positive_check",
            ),
            _same_school_target("section"),
            # Targets of composite foreign keys that pin a row's section, year, grade and school together
            # (enrollments use the full key, teacher assignments the year key).
            models.UniqueConstraint(
                fields=["id", "academic_year", "grade", "school"], name="academics_section_full_key_uniq"
            ),
            models.UniqueConstraint(
                fields=["id", "academic_year", "school"], name="academics_section_year_key_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["school", "academic_year", "grade"], name="academics_section_year_idx"),
        ]

    def __str__(self) -> str:
        return self.code


class SubjectCategory(models.TextChoices):
    CORE = "core", "Core"
    ELECTIVE = "elective", "Elective"
    LANGUAGE = "language", "Language"
    CO_CURRICULAR = "co_curricular", "Co-curricular"
    OTHER = "other", "Other"


class Subject(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=32)
    short_name = models.CharField(max_length=20, blank=True)
    description = models.CharField(max_length=500, blank=True)
    category = models.CharField(max_length=16, choices=SubjectCategory.choices, default=SubjectCategory.CORE)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="subjects"
    )
    status = models.CharField(max_length=16, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "academics_subject"
        constraints = [
            models.UniqueConstraint(fields=["school", "code"], name="academics_subject_code_uniq"),
            _unique_name("subject", "school"),
            _same_school_target("subject"),
        ]

    def __str__(self) -> str:
        return self.code
