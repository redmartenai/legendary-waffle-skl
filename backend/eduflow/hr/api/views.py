"""HR endpoints.

* Staff attendance: ``staff_attendance.read`` (self or school), ``staff_attendance.create`` (self check-in
  and check-out), ``staff_attendance.manage`` (office records and the late-after time).
* Leave: ``leave.read``, ``leave.request`` (own requests), ``leave.manage`` (leave types); decisions with
  ``leave.approve`` in the approvals queue.
* Payroll: ``payroll.read`` (own salary and payslips with self scope) and ``payroll.manage``.
* Recruitment: ``recruitment.read`` / ``recruitment.manage`` (school-wide).
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, DecimalField, QuerySet, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.parsers import JSONParser, MultiPartParser
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
from eduflow.documents import files
from eduflow.people.models import StaffProfile
from eduflow.tenancy import clock, domain

from .. import policies, services
from ..models import (
    Candidate,
    CandidateStage,
    JobOpening,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    PayrollRun,
    Payslip,
    StaffAttendance,
    StaffDayStatus,
)
from . import serializers as s

TAG = "hr"


def _school_wide(view: TenantAPIView, permission: str) -> None:
    if DataScope.SCHOOL not in view.actor.scopes(permission):
        view.permission_denied(view.request)


class SettingsView(TenantAPIView):
    required_permissions = {"GET": "staff_attendance.manage", "PUT": "staff_attendance.manage"}

    @extend_schema(
        tags=[TAG],
        summary="HR settings",
        parameters=[TENANT_HEADER],
        responses={200: s.HrSettingsOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        _school_wide(self, "staff_attendance.manage")
        return Response({"late_after": services.late_after(self.actor.school)})

    @extend_schema(
        tags=[TAG],
        summary="Set the time after which a check-in is late",
        parameters=[TENANT_HEADER],
        request=s.HrSettingsIn,
        responses={200: s.HrSettingsOut, **errors(400, 401, 403)},
    )
    def put(self, request: Request) -> Response:
        _school_wide(self, "staff_attendance.manage")
        body = s.HrSettingsIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            {"late_after": services.set_late_after(self.actor, body.validated_data["late_after"])}
        )


# ------------------------------------------------------------------------------------------------ attendance
class _Day(ResourceView):
    tag = TAG
    resource = policies.staff_days
    read_permission, write_permission = "staff_attendance.read", "staff_attendance.manage"
    output_serializer = s.StaffDayOut

    def base_queryset(self) -> QuerySet[StaffAttendance]:
        return StaffAttendance.objects.select_related("staff__membership__user")


@document_resource
class StaffDayList(_Day, ResourceListView):
    create_serializer = s.StaffDayIn
    filters = [
        Filter("staff_id", "staff_id", serializers.UUIDField()),
        Filter("status", "status", serializers.ChoiceField(StaffDayStatus.choices)),
        Filter("from", "date__gte", serializers.DateField()),
        Filter("to", "date__lte", serializers.DateField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> StaffAttendance:
        return services.record_day(self.actor, **data)


class _Check(TenantAPIView):
    required_permissions = {"POST": "staff_attendance.create"}

    def respond(self, day: StaffAttendance) -> Response:
        return Response(
            s.StaffDayOut(
                StaffAttendance.objects.select_related("staff__membership__user").get(pk=day.pk)
            ).data
        )


class CheckInView(_Check):
    @extend_schema(
        tags=[TAG],
        summary="Check in (the signed-in staff member)",
        description="After the school's late-after time (default 08:00) the day is `late`.",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.StaffDayOut, **errors(401, 403, 409)},
    )
    def post(self, request: Request) -> Response:
        return self.respond(services.check_in(self.actor))


class CheckOutView(_Check):
    @extend_schema(
        tags=[TAG],
        summary="Check out (the signed-in staff member)",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.StaffDayOut, **errors(401, 403, 409)},
    )
    def post(self, request: Request) -> Response:
        return self.respond(services.check_out(self.actor))


# ------------------------------------------------------------------------------------------------ leave
class _LeaveType(ResourceView):
    tag = TAG
    resource = policies.leave_types
    read_permission, write_permission = "leave.request", "leave.manage"
    output_serializer = s.LeaveTypeOut


@document_resource
class LeaveTypeList(_LeaveType, ResourceListView):
    create_serializer = s.LeaveTypeIn

    def perform_create(self, data: dict[str, Any]) -> LeaveType:
        leave_type = LeaveType(school=self.actor.school, **data)
        domain.save(leave_type, conflict="A leave type with this name already exists.")
        domain.record("hr.leave_type.created", leave_type, name=leave_type.name)
        return leave_type


@document_resource
class LeaveTypeDetail(_LeaveType, ResourceDetailView):
    update_serializer = s.LeaveTypeUpdateIn

    def perform_update(self, obj: LeaveType, data: dict[str, Any]) -> LeaveType:
        changed = domain.apply_changes(obj, data)
        if changed:
            domain.save(obj, conflict="A leave type with this name already exists.")
            domain.record("hr.leave_type.updated", obj, fields=changed)
        return obj


class _Leave(ResourceView):
    tag = TAG
    resource = policies.leave_requests
    read_permission, write_permission = "leave.read", "leave.request"
    output_serializer = s.LeaveOut
    narrow_writes = True  # a staff member requests their own leave (the service resolves who)

    def base_queryset(self) -> QuerySet[LeaveRequest]:
        return LeaveRequest.objects.select_related("staff__membership__user", "leave_type")


@document_resource
class LeaveList(_Leave, ResourceListView):
    create_serializer = s.LeaveIn
    filters = [
        Filter("staff_id", "staff_id", serializers.UUIDField()),
        Filter("status", "status", serializers.ChoiceField(LeaveStatus.choices)),
    ]

    def perform_create(self, data: dict[str, Any]) -> LeaveRequest:
        return services.request_leave(self.actor, **data)


@document_resource
class LeaveDetail(_Leave, ResourceDetailView):
    pass


class LeaveCancelView(TenantAPIView):
    required_permissions = {"POST": "leave.request"}

    @extend_schema(
        tags=[TAG],
        summary="Cancel your own pending leave request",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.LeaveOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        leave = policies.leave_requests.get(self.actor, "leave.request", pk, base=_Leave.base_queryset(self))  # type: ignore[arg-type]
        services.cancel_leave(self.actor, leave)
        leave.refresh_from_db()
        return Response(s.LeaveOut(leave).data)


class LeaveBalanceView(TenantAPIView):
    required_permissions = {"GET": "leave.request"}

    @extend_schema(
        tags=[TAG],
        summary="Your remaining leave, per active leave type, this academic year",
        parameters=[TENANT_HEADER],
        responses={200: s.BalanceOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        staff = services.own_staff(self.actor)
        today = clock.today(self.actor.school)
        rows = [
            {"leave_type": t, "remaining": services.balance(staff, t, today)}
            for t in LeaveType.objects.filter(school_id=self.actor.school.pk, is_active=True).order_by("name")
        ]
        return Response(s.BalanceOut(rows, many=True).data)


# ------------------------------------------------------------------------------------------------ payroll
class SalaryView(TenantAPIView):
    required_permissions = {"GET": "payroll.read", "PUT": "payroll.manage"}

    @extend_schema(
        tags=[TAG],
        summary="A staff member's salary structure",
        parameters=[TENANT_HEADER],
        responses={200: s.SalaryOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        base = policies.salaries.queryset(self.actor, "payroll.read").select_related(
            "staff__membership__user"
        )
        salary = base.filter(staff_id=pk).first()
        if salary is None:
            raise NotFound()
        return Response(s.SalaryOut(salary).data)

    @extend_schema(
        tags=[TAG],
        summary="Set a staff member's salary structure (entered amounts)",
        description="PF, ESI and TDS are entered amounts; EduFlow does not calculate statutory deductions.",
        parameters=[TENANT_HEADER],
        request=s.SalaryIn,
        responses={200: s.SalaryOut, **errors(400, 401, 403, 404)},
    )
    def put(self, request: Request, pk: Any) -> Response:
        _school_wide(self, "payroll.manage")
        staff = StaffProfile.objects.filter(school_id=self.actor.school.pk, pk=pk).first()
        if staff is None:
            raise NotFound()
        body = s.SalaryIn(data=request.data)
        body.is_valid(raise_exception=True)
        salary = services.set_salary(self.actor, staff, **body.validated_data)
        return Response(s.SalaryOut(salary).data)


class _Run(ResourceView):
    tag = TAG
    resource = policies.runs
    read_permission, write_permission = "payroll.manage", "payroll.manage"
    output_serializer = s.RunOut

    def base_queryset(self) -> QuerySet[PayrollRun]:
        money = DecimalField(max_digits=14, decimal_places=2)
        return PayrollRun.objects.annotate(
            gross=Coalesce(Sum("payslips__gross"), Value(0), output_field=money),
            deductions=Coalesce(Sum("payslips__deductions"), Value(0), output_field=money),
            net=Coalesce(Sum("payslips__net"), Value(0), output_field=money),
            staff_count=Count("payslips"),
        )


@document_resource
class RunList(_Run, ResourceListView):
    create_serializer = s.RunIn

    def perform_create(self, data: dict[str, Any]) -> PayrollRun:
        return services.create_run(self.actor, **data)


@document_resource
class RunDetail(_Run, ResourceDetailView):
    pass


class _RunAction(TenantAPIView):
    required_permissions = {"POST": "payroll.manage"}

    def run(self, pk: Any) -> PayrollRun:
        _school_wide(self, "payroll.manage")
        return policies.runs.get(self.actor, "payroll.manage", pk)

    def respond(self, run: PayrollRun) -> Response:
        return Response(s.RunOut(_Run.base_queryset(self).get(pk=run.pk)).data)  # type: ignore[arg-type]


class RunProcessView(_RunAction):
    @extend_schema(
        tags=[TAG],
        summary="Process the run: generate and freeze payslips from the salary structures",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.RunOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.respond(services.process_run(self.actor, self.run(pk)))


class RunPaidView(_RunAction):
    @extend_schema(
        tags=[TAG],
        summary="Mark a processed run as paid (staff are notified)",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.RunOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.respond(services.mark_paid(self.actor, self.run(pk)))


class _Payslip(ResourceView):
    tag = TAG
    resource = policies.payslips
    read_permission = "payroll.read"
    output_serializer = s.PayslipOut

    def base_queryset(self) -> QuerySet[Payslip]:
        return Payslip.objects.select_related("staff__membership__user", "run")


@document_resource
class PayslipList(_Payslip, ResourceListView):
    filters = [
        Filter("run_id", "run_id", serializers.UUIDField()),
        Filter("staff_id", "staff_id", serializers.UUIDField()),
    ]


@document_resource
class PayslipDetail(_Payslip, ResourceDetailView):
    pass


# ------------------------------------------------------------------------------------------------ recruitment
class _Opening(ResourceView):
    tag = TAG
    resource = policies.openings
    read_permission, write_permission = "recruitment.read", "recruitment.manage"
    output_serializer = s.OpeningOut

    def base_queryset(self) -> QuerySet[JobOpening]:
        return JobOpening.objects.select_related("department")


@document_resource
class OpeningList(_Opening, ResourceListView):
    create_serializer = s.OpeningIn

    def perform_create(self, data: dict[str, Any]) -> JobOpening:
        return services.create_opening(self.actor, **data)


@document_resource
class OpeningDetail(_Opening, ResourceDetailView):
    update_serializer = s.OpeningUpdateIn

    def perform_update(self, obj: JobOpening, data: dict[str, Any]) -> JobOpening:
        return services.update_opening(self.actor, obj, **data)


class _Candidate(ResourceView):
    tag = TAG
    resource = policies.candidates
    read_permission, write_permission = "recruitment.read", "recruitment.manage"
    output_serializer = s.CandidateOut

    def base_queryset(self) -> QuerySet[Candidate]:
        return Candidate.objects.select_related("resume")


@document_resource
class CandidateList(_Candidate, ResourceListView):
    create_serializer = s.CandidateIn
    parser_classes = [JSONParser, MultiPartParser]
    filters = [
        Filter("opening_id", "opening_id", serializers.UUIDField()),
        Filter("stage", "stage", serializers.ChoiceField(CandidateStage.choices)),
    ]

    def perform_create(self, data: dict[str, Any]) -> Candidate:
        return services.add_candidate(self.actor, **data)


@document_resource
class CandidateDetail(_Candidate, ResourceDetailView):
    update_serializer = s.CandidateUpdateIn

    def perform_update(self, obj: Candidate, data: dict[str, Any]) -> Candidate:
        return services.update_candidate(self.actor, obj, **data)


class CandidateResumeView(TenantAPIView):
    required_permissions = {"GET": "recruitment.read"}

    @extend_schema(
        tags=[TAG],
        summary="Download a candidate's resume",
        parameters=[TENANT_HEADER],
        responses={
            (200, "application/octet-stream"): OpenApiResponse(OpenApiTypes.BINARY),
            **errors(401, 403, 404),
        },
    )
    def get(self, request: Request, pk: Any) -> HttpResponse:
        candidate = policies.candidates.get(
            self.actor, "recruitment.read", pk, base=Candidate.objects.select_related("resume")
        )
        if candidate.resume is None:
            raise NotFound()
        return files.download(candidate.resume)
