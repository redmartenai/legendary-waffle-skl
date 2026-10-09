"""People, guardianship, enrollment and teaching endpoints."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.api.resources import (
    Filter,
    ResourceDetailView,
    ResourceListView,
    ResourceView,
    document_resource,
)
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors

from .. import policies, services
from ..models import (
    AssignmentStatus,
    Enrollment,
    EnrollmentStatus,
    Guardian,
    StaffProfile,
    StaffStatus,
    StaffType,
    Student,
    StudentGuardian,
    StudentStatus,
    TeacherAssignment,
)
from . import serializers as s

TAG = "people"
UUID = serializers.UUIDField


def _students_in(field: str) -> Any:
    """A filter over the student's *active* enrollment, without duplicating students."""

    def apply(qs: QuerySet[Student], value: Any) -> QuerySet[Student]:
        ids = Enrollment.objects.filter(status=EnrollmentStatus.ACTIVE, **{field: value}).values("student_id")
        return qs.filter(pk__in=ids)

    return apply


# ------------------------------------------------------------------------------------------------ staff
class _Staff(ResourceView):
    tag = TAG
    resource = policies.staff
    read_permission = "staff.read"
    output_serializer = s.StaffOut

    def base_queryset(self) -> QuerySet[StaffProfile]:
        return StaffProfile.objects.select_related("membership__user", "department", "campus")


@document_resource
class StaffList(_Staff, ResourceListView):
    write_permission = "staff.create"
    create_serializer = s.StaffCreateIn
    filters = [
        Filter(
            "staff_type",
            "staff_type",
            serializers.ChoiceField(StaffType.choices),
            "`teaching` lists teachers.",
        ),
        Filter("status", "status", serializers.ChoiceField(StaffStatus.choices)),
        Filter("department_id", "department_id", UUID()),
        Filter("campus_id", "campus_id", UUID()),
    ]

    def perform_create(self, data: dict[str, Any]) -> StaffProfile:
        return services.create_staff(self.actor, **data)


@document_resource
class StaffDetail(_Staff, ResourceDetailView):
    write_permission = "staff.update"
    update_serializer = s.StaffUpdateIn

    def perform_update(self, obj: StaffProfile, data: dict[str, Any]) -> StaffProfile:
        return services.update_staff(self.actor, obj, **data)


# ------------------------------------------------------------------------------------------------ students
class _Student(ResourceView):
    tag = TAG
    resource = policies.students
    read_permission = "student.read"
    output_serializer = s.StudentOut


@document_resource
class StudentList(_Student, ResourceListView):
    write_permission = "student.create"
    create_serializer = s.StudentIn
    filters = [
        Filter("status", "status", serializers.ChoiceField(StudentStatus.choices)),
        Filter(
            "section_id",
            "",
            UUID(),
            "Students actively enrolled in this section.",
            _students_in("section_id"),
        ),
        Filter(
            "academic_year_id",
            "",
            UUID(),
            "Students actively enrolled in this academic year.",
            _students_in("academic_year_id"),
        ),
        Filter("grade_id", "", UUID(), "Students actively enrolled in this grade.", _students_in("grade_id")),
    ]

    def perform_create(self, data: dict[str, Any]) -> Student:
        return services.create_student(self.actor, **data)


@document_resource
class StudentDetail(_Student, ResourceDetailView):
    write_permission = "student.update"
    update_serializer = s.StudentIn

    def perform_update(self, obj: Student, data: dict[str, Any]) -> Student:
        return services.update_student(self.actor, obj, **data)


# ------------------------------------------------------------------------------------------------ guardians
class _Guardian(ResourceView):
    tag = TAG
    resource = policies.guardians
    read_permission, write_permission = "guardian.read", "guardian.manage"
    output_serializer = s.GuardianOut


@document_resource
class GuardianList(_Guardian, ResourceListView):
    create_serializer = s.GuardianIn
    filters = [
        Filter(
            "student_id",
            "",
            UUID(),
            "Guardians linked to this student.",
            lambda qs, value: qs.filter(
                pk__in=StudentGuardian.objects.filter(student_id=value).values("guardian_id")
            ),
        )
    ]

    def perform_create(self, data: dict[str, Any]) -> Guardian:
        return services.create_guardian(self.actor, **data)


@document_resource
class GuardianDetail(_Guardian, ResourceDetailView):
    update_serializer = s.GuardianIn

    def perform_update(self, obj: Guardian, data: dict[str, Any]) -> Guardian:
        return services.update_guardian(self.actor, obj, **data)


class _Link(ResourceView):
    tag = TAG
    resource = policies.student_guardians
    read_permission, write_permission = "guardian.read", "guardian.manage"
    output_serializer = s.StudentGuardianOut

    def base_queryset(self) -> QuerySet[StudentGuardian]:
        return StudentGuardian.objects.select_related("student", "guardian")


@document_resource
class StudentGuardianList(_Link, ResourceListView):
    create_serializer = s.StudentGuardianCreateIn
    filters = [Filter("student_id", "student_id", UUID()), Filter("guardian_id", "guardian_id", UUID())]

    def perform_create(self, data: dict[str, Any]) -> StudentGuardian:
        return services.link_guardian(self.actor, **data)


@document_resource
class StudentGuardianDetail(_Link, ResourceDetailView):
    update_serializer = s.StudentGuardianUpdateIn
    allow_delete = True

    def perform_update(self, obj: StudentGuardian, data: dict[str, Any]) -> StudentGuardian:
        return services.update_guardian_link(self.actor, obj, **data)

    def perform_delete(self, obj: StudentGuardian) -> None:
        services.unlink_guardian(self.actor, obj)


# ------------------------------------------------------------------------------------------------ enrollments
class _Enrollment(ResourceView):
    tag = TAG
    resource = policies.enrollments
    read_permission, write_permission = "enrollment.read", "enrollment.manage"
    output_serializer = s.EnrollmentOut

    def base_queryset(self) -> QuerySet[Enrollment]:
        return Enrollment.objects.select_related("student", "academic_year", "grade", "section")


@document_resource
class EnrollmentList(_Enrollment, ResourceListView):
    create_serializer = s.EnrollmentCreateIn
    filters = [
        Filter("student_id", "student_id", UUID()),
        Filter("section_id", "section_id", UUID()),
        Filter("academic_year_id", "academic_year_id", UUID()),
        Filter("grade_id", "grade_id", UUID()),
        Filter("status", "status", serializers.ChoiceField(EnrollmentStatus.choices)),
    ]

    def perform_create(self, data: dict[str, Any]) -> Enrollment:
        return services.enroll(self.actor, **data)


@document_resource
class EnrollmentDetail(_Enrollment, ResourceDetailView):
    update_serializer = s.EnrollmentUpdateIn

    def perform_update(self, obj: Enrollment, data: dict[str, Any]) -> Enrollment:
        return services.update_enrollment(self.actor, obj, **data)


class _EnrollmentAction(TenantAPIView):
    required_permissions = {"POST": "enrollment.manage"}

    def load(self, pk: Any) -> Enrollment:
        if DataScope.SCHOOL not in self.actor.scopes("enrollment.manage"):
            self.permission_denied(self.request)
        return policies.enrollments.get(
            self.actor, "enrollment.manage", pk, base=Enrollment.objects.select_related("academic_year")
        )

    def respond(self, enrollment: Enrollment) -> Response:
        fresh = Enrollment.objects.select_related("student", "academic_year", "grade", "section").get(
            pk=enrollment.pk
        )
        return Response(s.EnrollmentOut(fresh).data)


class EnrollmentEndView(_EnrollmentAction):
    @extend_schema(
        tags=[TAG],
        summary="Complete or withdraw an enrollment",
        description="Requires `enrollment.manage`. Only an active enrollment can end; the change is final.",
        parameters=[TENANT_HEADER],
        request=s.EnrollmentEndIn,
        responses={200: s.EnrollmentOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        body = s.EnrollmentEndIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.end_enrollment(self.actor, self.load(pk), **body.validated_data))


class EnrollmentTransferView(_EnrollmentAction):
    @extend_schema(
        tags=[TAG],
        summary="Transfer a student to another section of the same academic year",
        description=(
            "Requires `enrollment.manage`. Marks this enrollment `transferred` and returns the new "
            "active one."
        ),
        parameters=[TENANT_HEADER],
        request=s.EnrollmentTransferIn,
        responses={200: s.EnrollmentOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        body = s.EnrollmentTransferIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.transfer_enrollment(self.actor, self.load(pk), **body.validated_data))


# ------------------------------------------------------------------------------------------------ assignments
class _Assignment(ResourceView):
    tag = TAG
    resource = policies.teacher_assignments
    read_permission, write_permission = "teacher_assignment.read", "teacher_assignment.manage"
    output_serializer = s.TeacherAssignmentOut

    def base_queryset(self) -> QuerySet[TeacherAssignment]:
        return TeacherAssignment.objects.select_related(
            "staff__membership__user", "academic_year", "section", "subject"
        )


@document_resource
class TeacherAssignmentList(_Assignment, ResourceListView):
    create_serializer = s.TeacherAssignmentCreateIn
    filters = [
        Filter("staff_id", "staff_id", UUID()),
        Filter("section_id", "section_id", UUID()),
        Filter("subject_id", "subject_id", UUID()),
        Filter("academic_year_id", "academic_year_id", UUID()),
        Filter("status", "status", serializers.ChoiceField(AssignmentStatus.choices)),
        Filter("is_class_teacher", "is_class_teacher", serializers.BooleanField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> TeacherAssignment:
        return services.assign_teacher(self.actor, **data)


@document_resource
class TeacherAssignmentDetail(_Assignment, ResourceDetailView):
    update_serializer = s.TeacherAssignmentUpdateIn
    allow_delete = True

    def perform_update(self, obj: TeacherAssignment, data: dict[str, Any]) -> TeacherAssignment:
        return services.update_assignment(self.actor, obj, **data)

    def perform_delete(self, obj: TeacherAssignment) -> None:
        services.delete_assignment(self.actor, obj)
