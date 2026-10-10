"""Fee endpoints. Plans and obligations: ``fee.read`` / ``fee.manage``; payments and refund requests:
``fee.update`` (school-wide); refund decisions: ``fee.approve`` in the approvals queue. Families read their
children's statements, payments and receipts (``fee.read`` child / self)."""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import QuerySet, Sum
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
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
from eduflow.people import policies as people_policies
from eduflow.tenancy import clock

from .. import policies, services
from ..models import FeePlan, Payment, PaymentMode, StudentFee
from . import serializers as s

TAG = "fees"


def _school_wide(view: TenantAPIView, permission: str) -> None:
    if DataScope.SCHOOL not in view.actor.scopes(permission):
        view.permission_denied(view.request)


# ------------------------------------------------------------------------------------------------ plans
class _Plan(ResourceView):
    tag = TAG
    resource = policies.plans
    read_permission, write_permission = "fee.read", "fee.manage"
    output_serializer = s.PlanOut

    def base_queryset(self) -> QuerySet[FeePlan]:
        return (
            FeePlan.objects.select_related("academic_year", "grade")
            .prefetch_related("instalments")
            .annotate(total=Sum("instalments__amount"))
        )


@document_resource
class PlanList(_Plan, ResourceListView):
    create_serializer = s.PlanIn
    filters = [Filter("academic_year_id", "academic_year_id", serializers.UUIDField())]

    def perform_create(self, data: dict[str, Any]) -> FeePlan:
        return services.create_plan(self.actor, **data)


@document_resource
class PlanDetail(_Plan, ResourceDetailView):
    update_serializer = s.PlanUpdateIn

    def perform_update(self, obj: FeePlan, data: dict[str, Any]) -> FeePlan:
        return services.update_plan(self.actor, obj, **data)


class PlanAssignView(TenantAPIView):
    required_permissions = {"POST": "fee.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Assign the plan to every active student of a section",
        parameters=[TENANT_HEADER],
        request=s.AssignSectionIn,
        responses={200: s.AssignedOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self, "fee.manage")
        plan = policies.plans.get(self.actor, "fee.manage", pk)
        body = s.AssignSectionIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response({"assigned": services.assign_section(self.actor, plan, **body.validated_data)})


# ------------------------------------------------------------------------------------------------ obligations
class _StudentFee(ResourceView):
    tag = TAG
    resource = policies.student_fees
    read_permission, write_permission = "fee.read", "fee.manage"
    output_serializer = s.StudentFeeOut

    def base_queryset(self) -> QuerySet[StudentFee]:
        return StudentFee.objects.select_related("student", "plan")


@document_resource
class StudentFeeList(_StudentFee, ResourceListView):
    create_serializer = s.StudentFeeIn
    filters = [
        Filter("student_id", "student_id", serializers.UUIDField()),
        Filter("plan_id", "plan_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> StudentFee:
        return services.assign(self.actor, **data)


@document_resource
class StudentFeeDetail(_StudentFee, ResourceDetailView):
    update_serializer = s.StudentFeeUpdateIn

    def perform_update(self, obj: StudentFee, data: dict[str, Any]) -> StudentFee:
        return services.update_obligation(self.actor, obj, **data)


# ------------------------------------------------------------------------------------------------ payments
class _Payment(ResourceView):
    tag = TAG
    resource = policies.payments
    read_permission, write_permission = "fee.read", "fee.update"
    output_serializer = s.PaymentOut

    def base_queryset(self) -> QuerySet[Payment]:
        return Payment.objects.select_related("student", "collected_by__user")


@document_resource
class PaymentList(_Payment, ResourceListView):
    create_serializer = s.PaymentIn
    filters = [
        Filter("student_id", "student_id", serializers.UUIDField()),
        Filter("mode", "mode", serializers.ChoiceField(PaymentMode.choices)),
        Filter("from", "paid_on__gte", serializers.DateField()),
        Filter("to", "paid_on__lte", serializers.DateField()),
        Filter("receipt_number", "receipt_number", serializers.CharField()),
    ]

    def post(self, request: Request) -> Response:
        self.require_school_scope(request, "fee.update")
        body = s.PaymentIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        data["client_key"] = data.get("client_key") or request.headers.get("Idempotency-Key", "")
        payment, replayed = services.record_payment(self.actor, **data)
        return Response(self.out(payment), status=200 if replayed else 201)


@document_resource
class PaymentDetail(_Payment, ResourceDetailView):
    pass


class RefundRequestView(TenantAPIView):
    required_permissions = {"GET": "fee.read", "POST": "fee.update"}

    @extend_schema(
        tags=[TAG],
        summary="Refunds of a payment",
        parameters=[TENANT_HEADER],
        responses={200: s.RefundOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        payment = policies.payments.get(self.actor, "fee.read", pk)
        return Response(s.RefundOut(payment.refunds.select_related("payment"), many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Request a refund against a payment",
        description="Decided in the approvals queue (`refund`, needs `fee.approve`).",
        parameters=[TENANT_HEADER],
        request=s.RefundIn,
        responses={201: s.RefundOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self, "fee.update")
        payment = policies.payments.get(self.actor, "fee.update", pk)
        body = s.RefundIn(data=request.data)
        body.is_valid(raise_exception=True)
        refund = services.request_refund(self.actor, payment, **body.validated_data)
        return Response(s.RefundOut(refund).data, status=201)


# ------------------------------------------------------------------------------------------------ reports
class StudentStatementView(TenantAPIView):
    required_permissions = {"GET": "fee.read"}

    @extend_schema(
        tags=[TAG],
        summary="A student's fee statement",
        description="Instalments after scholarship, paid, refunded, balance and overdue as of today.",
        parameters=[TENANT_HEADER],
        responses={200: s.StatementOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        student = people_policies.students.get(self.actor, "fee.read", pk)
        return Response(s.StatementOut(services.statement(student, clock.today(self.actor.school))).data)


class DefaultersView(TenantAPIView):
    required_permissions = {"GET": "fee.read"}

    @extend_schema(
        tags=[TAG],
        summary="Students with overdue fees, largest first",
        parameters=[
            TENANT_HEADER,
            OpenApiParameter("section_id", OpenApiTypes.UUID),
            OpenApiParameter("min_overdue", OpenApiTypes.DECIMAL),
        ],
        responses={200: s.StatementOut(many=True), **errors(400, 401, 403)},
    )
    def get(self, request: Request) -> Response:
        _school_wide(self, "fee.read")
        students = people_policies.students.queryset(self.actor, "fee.read").filter(status="active")
        section_id = request.query_params.get("section_id")
        if section_id:
            section = serializers.UUIDField().run_validation(section_id)
            students = students.filter(enrollments__section_id=section, enrollments__status="active")
        minimum = serializers.DecimalField(max_digits=12, decimal_places=2).run_validation(
            request.query_params.get("min_overdue") or "0"
        )
        rows = [
            r for r in services.defaulters(students, clock.today(self.actor.school)) if r.overdue >= minimum
        ]
        return Response(s.StatementOut(rows, many=True).data)


class CollectionsView(TenantAPIView):
    required_permissions = {"GET": "fee.read"}

    @extend_schema(
        tags=[TAG],
        summary="Money received in a period, by mode, net of approved refunds",
        parameters=[
            TENANT_HEADER,
            OpenApiParameter("from", OpenApiTypes.DATE, required=True),
            OpenApiParameter("to", OpenApiTypes.DATE, required=True),
        ],
        responses={200: s.CollectionsOut, **errors(400, 401, 403)},
    )
    def get(self, request: Request) -> Response:
        _school_wide(self, "fee.read")
        field = serializers.DateField()
        since: datetime.date = field.run_validation(request.query_params.get("from"))
        until: datetime.date = field.run_validation(request.query_params.get("to"))
        return Response(s.CollectionsOut(services.collections(self.actor.school, since, until)).data)
