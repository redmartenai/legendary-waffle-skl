"""Exams, mark sheets, marks entry, corrections, results and report cards.

Exams and grade bands: ``exam.read`` (everyone, school-wide) and ``exam.manage`` (office). Mark sheets and
marks: ``assessment.read`` scoped to the section / student, and families only see published marks. Teachers
enter and submit marks (``assessment.create``) and request corrections (``assessment.update``) for a section
and subject they teach. Approval of sheets and corrections is in the central approvals queue.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
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
from eduflow.people import policies as people_policies
from eduflow.tenancy import domain

from .. import policies, services
from ..models import Exam, GradeBand, Mark, MarkSheet, SheetStatus
from . import serializers as s

TAG = "assessment"


# ------------------------------------------------------------------------------------------------ exams
class _Exam(ResourceView):
    tag = TAG
    resource = policies.exams
    read_permission, write_permission = "exam.read", "exam.manage"
    output_serializer = s.ExamOut

    def base_queryset(self) -> QuerySet[Exam]:
        return Exam.objects.select_related("academic_year", "term")


@document_resource
class ExamList(_Exam, ResourceListView):
    create_serializer = s.ExamIn
    filters = [
        Filter("academic_year_id", "academic_year_id", serializers.UUIDField()),
        Filter("term_id", "term_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Exam:
        return services.create_exam(self.actor, **data)


@document_resource
class ExamDetail(_Exam, ResourceDetailView):
    update_serializer = s.ExamUpdateIn

    def perform_update(self, obj: Exam, data: dict[str, Any]) -> Exam:
        return services.update_exam(self.actor, obj, **data)


class _ExamAction(TenantAPIView):
    def exam(self, pk: Any, permission: str) -> Exam:
        if DataScope.SCHOOL not in self.actor.scopes(permission):
            self.permission_denied(self.request)
        return policies.exams.get(self.actor, permission, pk)


def _sheets(view: TenantAPIView) -> QuerySet[MarkSheet]:
    base = MarkSheet.objects.select_related("section", "subject", "teacher__membership__user")
    return policies.sheets.queryset(view.actor, "assessment.read", base)


class ExamSheetsView(_ExamAction):
    required_permissions = {"GET": "assessment.read", "POST": "exam.manage"}

    @extend_schema(
        tags=[TAG],
        summary="The exam's mark sheets the caller may see",
        parameters=[TENANT_HEADER],
        responses={200: s.SheetOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        exam = policies.exams.get(self.actor, "exam.read", pk)
        rows = _sheets(self).filter(exam=exam).order_by("section__code", "subject__name")
        return Response(s.SheetOut(rows, many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Add a mark sheet (section x subject) to the exam",
        parameters=[TENANT_HEADER],
        request=s.SheetIn,
        responses={201: s.SheetOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        exam = self.exam(pk, "exam.manage")
        body = s.SheetIn(data=request.data)
        body.is_valid(raise_exception=True)
        sheet = services.create_sheet(self.actor, exam, **body.validated_data)
        return Response(s.SheetOut(sheet).data, status=201)


class ExamGenerateView(_ExamAction):
    required_permissions = {"POST": "exam.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Create a sheet for every subject-teacher assignment of the exam's year",
        description="Existing sheets are kept. Each sheet is assigned to the teacher of that assignment.",
        parameters=[TENANT_HEADER],
        request=s.SheetGenerateIn,
        responses={200: s.SheetsGeneratedOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        exam = self.exam(pk, "exam.manage")
        body = s.SheetGenerateIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response({"created": len(services.generate_sheets(self.actor, exam, **body.validated_data))})


class ExamPublishView(_ExamAction):
    required_permissions = {"POST": "exam.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Publish every approved mark sheet of the exam",
        description="Families are notified. Sheets that are not approved stay unpublished.",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.PublishedOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        exam = self.exam(pk, "exam.manage")
        return Response({"published_sheets": services.publish_exam(self.actor, exam)})


class ExamResultsView(TenantAPIView):
    required_permissions = {"GET": "assessment.read"}

    @extend_schema(
        tags=[TAG],
        summary="Exam results of a section, ranked",
        description="Totals over the marks the caller may see. Families see published marks only.",
        parameters=[TENANT_HEADER, OpenApiParameter("section_id", OpenApiTypes.UUID, required=True)],
        responses={200: s.ResultRowOut(many=True), **errors(400, 401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        exam = policies.exams.get(self.actor, "exam.read", pk)
        section_id = serializers.UUIDField().run_validation(request.query_params.get("section_id") or None)
        marks = policies.marks.queryset(self.actor, "assessment.read").filter(
            sheet__exam=exam, sheet__section_id=section_id
        )
        students = (
            people_policies.students.queryset(self.actor, "assessment.read")
            .filter(marks__in=marks)
            .distinct()
        )
        cards = [services.report_card(st, exam, marks) for st in students]
        cards.sort(key=lambda c: c["percent"] if c["percent"] is not None else Decimal(-1), reverse=True)
        rows = [{**c, "rank": i + 1} for i, c in enumerate(cards)]
        return Response(s.ResultRowOut(rows, many=True).data)


# ------------------------------------------------------------------------------------------------ sheets
def _sheet_detail(view: TenantAPIView, sheet: MarkSheet) -> dict[str, Any]:
    visible = people_policies.students.queryset(view.actor, "assessment.read", base=services.roster(sheet))
    marks = {
        m.student_id: m for m in policies.marks.queryset(view.actor, "assessment.read").filter(sheet=sheet)
    }
    entries = []
    for student in visible.order_by("first_name", "last_name"):
        mark = marks.get(student.pk)
        if mark is None and sheet.status == SheetStatus.PUBLISHED:
            continue
        entries.append(
            {
                "mark_id": mark.pk if mark else None,
                "student": student,
                "marks": mark.marks if mark else None,
                "absent": mark.absent if mark else False,
                "remark": mark.remark if mark else "",
            }
        )
    fresh = MarkSheet.objects.select_related("section", "subject", "teacher__membership__user").get(
        pk=sheet.pk
    )
    data = dict(s.SheetOut(fresh).data)
    data["entries"] = s.EntryOut(entries, many=True).data
    return data


class MarkSheetDetailView(TenantAPIView):
    required_permissions = {"GET": "assessment.read"}

    @extend_schema(
        tags=[TAG],
        summary="A mark sheet with the marks the caller may see",
        parameters=[TENANT_HEADER],
        responses={200: s.SheetDetailOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        return Response(_sheet_detail(self, policies.sheets.get(self.actor, "assessment.read", pk)))


class MarkSheetMarksView(TenantAPIView):
    required_permissions = {"PUT": "assessment.create"}

    @extend_schema(
        tags=[TAG],
        summary="Enter or change marks on a draft sheet",
        description="Teachers: a section and subject they teach. Each entry gives marks or `absent`.",
        parameters=[TENANT_HEADER],
        request=s.MarksIn,
        responses={200: s.SheetDetailOut, **errors(400, 401, 403, 404, 409)},
    )
    def put(self, request: Request, pk: Any) -> Response:
        sheet = policies.sheets.get(self.actor, "assessment.create", pk)
        body = s.MarksIn(data=request.data)
        body.is_valid(raise_exception=True)
        services.enter_marks(self.actor, sheet, body.validated_data["marks"])
        return Response(_sheet_detail(self, sheet))


class MarkSheetSubmitView(TenantAPIView):
    required_permissions = {"POST": "assessment.create"}

    @extend_schema(
        tags=[TAG],
        summary="Submit a complete draft sheet for approval",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.SheetOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        sheet = policies.sheets.get(self.actor, "assessment.create", pk)
        services.submit_sheet(self.actor, sheet)
        return Response(_sheet_detail(self, sheet))


class MarkCorrectionView(TenantAPIView):
    required_permissions = {"POST": "assessment.update"}

    @extend_schema(
        tags=[TAG],
        summary="Request a correction of an approved or published mark",
        description="Decided in the approvals queue (`marks_correction`).",
        parameters=[TENANT_HEADER],
        request=s.MarkCorrectionIn,
        responses={201: s.MarkCorrectionOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        base = Mark.objects.select_related("sheet__section", "sheet__subject")
        mark = policies.marks.get(self.actor, "assessment.update", pk, base=base)
        body = s.MarkCorrectionIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.MarkCorrectionOut(services.request_correction(self.actor, mark, **body.validated_data)).data,
            status=201,
        )


class ReportCardView(TenantAPIView):
    required_permissions = {"GET": "assessment.read"}

    @extend_schema(
        tags=[TAG],
        summary="A student's report card for one exam",
        description=(
            "Lines per subject with percentage and the school's grade band (none when the school has not "
            "defined bands). The student must be visible under `student.read` and `assessment.read`."
        ),
        parameters=[TENANT_HEADER, OpenApiParameter("exam_id", OpenApiTypes.UUID, required=True)],
        responses={200: s.ReportCardOut, **errors(400, 401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        student = people_policies.students.get(self.actor, "assessment.read", pk)
        if not people_policies.students.can(self.actor, "student.read", student):
            raise NotFound()
        exam_id = request.query_params.get("exam_id")
        if not exam_id:
            raise ValidationError({"exam_id": ["This query parameter is required."]})
        exam = domain.resolve(
            Exam, self.actor.school, serializers.UUIDField().run_validation(exam_id), "exam_id"
        )
        marks = policies.marks.queryset(self.actor, "assessment.read")
        return Response(s.ReportCardOut(services.report_card(student, exam, marks)).data)


# ------------------------------------------------------------------------------------------------ bands
class _Band(ResourceView):
    tag = TAG
    resource = policies.grade_bands
    read_permission, write_permission = "exam.read", "exam.manage"
    output_serializer = s.GradeBandOut


@document_resource
class GradeBandList(_Band, ResourceListView):
    create_serializer = s.GradeBandIn

    def perform_create(self, data: dict[str, Any]) -> GradeBand:
        band = GradeBand(school=self.actor.school, **data)
        domain.save(band, conflict="A band with this label or minimum already exists.")
        domain.record("assessment.grade_band.created", band, label=band.label)
        return band


@document_resource
class GradeBandDetail(_Band, ResourceDetailView):
    update_serializer = s.GradeBandUpdateIn
    allow_delete = True

    def perform_update(self, obj: GradeBand, data: dict[str, Any]) -> GradeBand:
        changed = domain.apply_changes(obj, data)
        if changed:
            domain.save(obj, conflict="A band with this label or minimum already exists.")
            domain.record("assessment.grade_band.updated", obj, fields=changed)
        return obj

    def perform_delete(self, obj: GradeBand) -> None:
        domain.record("assessment.grade_band.deleted", obj, label=obj.label)
        domain.delete(obj)
