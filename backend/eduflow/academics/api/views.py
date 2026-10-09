"""Academic-structure endpoints. Thin: authorization in the base views, rules in services."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from rest_framework import serializers

from eduflow.authz.api.resources import (
    Filter,
    ResourceDetailView,
    ResourceListView,
    ResourceView,
    document_resource,
)

from .. import policies, services
from ..models import (
    AcademicYear,
    AcademicYearStatus,
    Campus,
    Department,
    Grade,
    RecordStatus,
    Section,
    Subject,
    SubjectCategory,
)
from . import serializers as s

TAG = "academics"
STATUS = Filter("status", "status", serializers.ChoiceField(RecordStatus.choices))


# ------------------------------------------------------------------------------------------------ campuses
class _Campus(ResourceView):
    tag = TAG
    resource = policies.campuses
    read_permission, write_permission = "campus.read", "campus.manage"
    output_serializer = s.CampusOut


@document_resource
class CampusList(_Campus, ResourceListView):
    create_serializer = s.CampusIn
    filters = [STATUS]

    def perform_create(self, data: dict[str, Any]) -> Campus:
        return services.create_campus(self.actor, **data)


@document_resource
class CampusDetail(_Campus, ResourceDetailView):
    update_serializer = s.CampusIn
    allow_delete = True

    def perform_update(self, obj: Campus, data: dict[str, Any]) -> Campus:
        return services.update_campus(self.actor, obj, **data)

    def perform_delete(self, obj: Campus) -> None:
        services.delete_campus(obj)


# ------------------------------------------------------------------------------------------------ years
class _Year(ResourceView):
    tag = TAG
    resource = policies.academic_years
    read_permission, write_permission = "academic_year.read", "academic_year.manage"
    output_serializer = s.AcademicYearOut


@document_resource
class AcademicYearList(_Year, ResourceListView):
    create_serializer = s.AcademicYearCreateIn
    filters = [
        Filter("status", "status", serializers.ChoiceField(AcademicYearStatus.choices)),
        Filter("is_current", "is_current", serializers.BooleanField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> AcademicYear:
        return services.create_academic_year(self.actor, **data)


@document_resource
class AcademicYearDetail(_Year, ResourceDetailView):
    update_serializer = s.AcademicYearUpdateIn
    allow_delete = True

    def perform_update(self, obj: AcademicYear, data: dict[str, Any]) -> AcademicYear:
        return services.update_academic_year(self.actor, obj, **data)

    def perform_delete(self, obj: AcademicYear) -> None:
        services.delete_academic_year(self.actor, obj)


# ------------------------------------------------------------------------------------------------ departments
class _Department(ResourceView):
    tag = TAG
    resource = policies.departments
    read_permission, write_permission = "department.read", "department.manage"
    output_serializer = s.DepartmentOut


@document_resource
class DepartmentList(_Department, ResourceListView):
    create_serializer = s.DepartmentIn
    filters = [STATUS]

    def perform_create(self, data: dict[str, Any]) -> Department:
        return services.create_department(self.actor, **data)


@document_resource
class DepartmentDetail(_Department, ResourceDetailView):
    update_serializer = s.DepartmentIn
    allow_delete = True

    def perform_update(self, obj: Department, data: dict[str, Any]) -> Department:
        return services.update_department(self.actor, obj, **data)

    def perform_delete(self, obj: Department) -> None:
        services.delete_department(obj)


# ------------------------------------------------------------------------------------------------ grades
class _Grade(ResourceView):
    tag = TAG
    resource = policies.grades
    read_permission, write_permission = "grade.read", "grade.manage"
    output_serializer = s.GradeOut


@document_resource
class GradeList(_Grade, ResourceListView):
    create_serializer = s.GradeIn
    filters = [STATUS]

    def perform_create(self, data: dict[str, Any]) -> Grade:
        return services.create_grade(self.actor, **data)


@document_resource
class GradeDetail(_Grade, ResourceDetailView):
    update_serializer = s.GradeIn
    allow_delete = True

    def perform_update(self, obj: Grade, data: dict[str, Any]) -> Grade:
        return services.update_grade(self.actor, obj, **data)

    def perform_delete(self, obj: Grade) -> None:
        services.delete_grade(obj)


# ------------------------------------------------------------------------------------------------ sections
class _Section(ResourceView):
    tag = TAG
    resource = policies.sections
    read_permission, write_permission = "section.read", "section.manage"
    output_serializer = s.SectionOut

    def base_queryset(self) -> QuerySet[Section]:
        return Section.objects.select_related("academic_year", "grade", "campus")


@document_resource
class SectionList(_Section, ResourceListView):
    create_serializer = s.SectionCreateIn
    filters = [
        Filter("academic_year_id", "academic_year_id", serializers.UUIDField()),
        Filter("grade_id", "grade_id", serializers.UUIDField()),
        Filter("campus_id", "campus_id", serializers.UUIDField()),
        STATUS,
    ]

    def perform_create(self, data: dict[str, Any]) -> Section:
        return services.create_section(self.actor, **data)


@document_resource
class SectionDetail(_Section, ResourceDetailView):
    update_serializer = s.SectionUpdateIn
    allow_delete = True

    def perform_update(self, obj: Section, data: dict[str, Any]) -> Section:
        return services.update_section(self.actor, obj, **data)

    def perform_delete(self, obj: Section) -> None:
        services.delete_section(obj)


# ------------------------------------------------------------------------------------------------ subjects
class _Subject(ResourceView):
    tag = TAG
    resource = policies.subjects
    read_permission, write_permission = "subject.read", "subject.manage"
    output_serializer = s.SubjectOut

    def base_queryset(self) -> QuerySet[Subject]:
        return Subject.objects.select_related("department")


@document_resource
class SubjectList(_Subject, ResourceListView):
    create_serializer = s.SubjectIn
    filters = [
        STATUS,
        Filter("category", "category", serializers.ChoiceField(SubjectCategory.choices)),
        Filter("department_id", "department_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Subject:
        return services.create_subject(self.actor, **data)


@document_resource
class SubjectDetail(_Subject, ResourceDetailView):
    update_serializer = s.SubjectIn
    allow_delete = True

    def perform_update(self, obj: Subject, data: dict[str, Any]) -> Subject:
        return services.update_subject(self.actor, obj, **data)

    def perform_delete(self, obj: Subject) -> None:
        services.delete_subject(obj)
