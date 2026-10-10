"""Reports: ``GET /reports/<name>`` as JSON, or ``?export=csv`` to download. Every report needs
``report.read`` **and** the permission of the data it shows, and reads through the caller's data scopes
(a teacher's attendance report covers the students they teach). Every report run is audited
(``reports.<name>.exported``)."""

from __future__ import annotations

import csv
import datetime
import io
from collections.abc import Callable
from typing import Any

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.audit import services as audit
from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors
from eduflow.tenancy import clock

from .. import builders

TAG = "reports"
MAX_DAYS = 366

TableOut = inline_serializer(
    "ReportTable",
    fields={
        "columns": serializers.ListField(child=serializers.CharField()),
        "rows": serializers.ListField(
            child=serializers.ListField(child=serializers.CharField(allow_null=True))
        ),
    },
)
FORMAT = OpenApiParameter(
    "export", OpenApiTypes.STR, enum=["csv"], description="csv: download a CSV file instead of JSON."
)
SECTION = OpenApiParameter("section_id", OpenApiTypes.UUID)
FROM = OpenApiParameter("from", OpenApiTypes.DATE, description="Default: 30 days ago.")
TO = OpenApiParameter("to", OpenApiTypes.DATE, description="Default: today.")


def _period(view: TenantAPIView, request: Request) -> dict[str, datetime.date]:
    field = serializers.DateField()
    today = clock.today(view.actor.school)
    until = field.run_validation(request.query_params.get("to")) if request.query_params.get("to") else today
    since = (
        field.run_validation(request.query_params.get("from"))
        if request.query_params.get("from")
        else until - datetime.timedelta(days=30)
    )
    if since > until or (until - since).days > MAX_DAYS:
        raise ValidationError({"from": [f"Give a period of at most {MAX_DAYS} days, from before to."]})
    return {"since": since, "until": until}


def _section(request: Request) -> dict[str, Any]:
    raw = request.query_params.get("section_id")
    return {"section_id": serializers.UUIDField().run_validation(raw)} if raw else {}


def _render(
    view: TenantAPIView, request: Request, name: str, table: builders.Table
) -> HttpResponse | Response:
    as_csv = request.query_params.get("export") == "csv"
    audit.record(
        f"reports.{name}.exported",
        target_type="report",
        target_id=name,
        metadata={"rows": len(table.rows), "format": "csv" if as_csv else "json"},
    )
    if not as_csv:
        return Response(
            {
                "columns": table.columns,
                "rows": [[None if v is None else str(v) for v in r] for r in table.rows],
            }
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(table.columns)
    for row in table.rows:
        # Spreadsheet formula injection: neutralise cells that would start a formula.
        writer.writerow(
            [
                "'" + str(v)
                if str(v)[:1] in ("=", "+", "-", "@") and not _number(v)
                else ("" if v is None else v)
                for v in row
            ]
        )
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}-{clock.today(view.actor.school)}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response


def _number(value: Any) -> bool:
    try:
        float(str(value))
    except ValueError:
        return False
    return True


def _report(
    name: str,
    permission: str,
    summary: str,
    params: list[OpenApiParameter],
    build: Callable[[TenantAPIView, Request], builders.Table],
) -> type[TenantAPIView]:
    class ReportView(TenantAPIView):
        required_permissions = {"GET": "report.read"}

        @extend_schema(
            tags=[TAG],
            operation_id=f"report_{name.replace('-', '_')}",
            summary=summary,
            description=f"Requires `report.read` and `{permission}`; covers what the caller may see.",
            parameters=[TENANT_HEADER, FORMAT, *params],
            responses={
                200: TableOut,
                (200, "text/csv"): OpenApiResponse(OpenApiTypes.BINARY),
                **errors(400, 401, 403),
            },
        )
        def get(self, request: Request) -> HttpResponse | Response:
            if not self.actor.scopes(permission):
                self.permission_denied(request)
            return _render(self, request, name, build(self, request))

    ReportView.__name__ = ReportView.__qualname__ = f"{name.title().replace('-', '')}Report"
    return ReportView


def _school_only(view: TenantAPIView, permission: str) -> None:
    if DataScope.SCHOOL not in view.actor.scopes(permission):
        view.permission_denied(view.request)


AttendanceReport = _report(
    "attendance",
    "attendance.read",
    "Attendance by student for a period",
    [FROM, TO, SECTION],
    lambda v, r: builders.attendance(v.actor, **_period(v, r), **_section(r)),
)
FeeDuesReport = _report(
    "fee-dues",
    "fee.read",
    "Fee dues by student",
    [SECTION],
    lambda v, r: builders.fee_dues(v.actor, **_section(r)),
)
ExamResultsReport = _report(
    "exam-results",
    "assessment.read",
    "Exam results by student and subject",
    [OpenApiParameter("exam_id", OpenApiTypes.UUID, required=True), SECTION],
    lambda v, r: builders.exam_results(
        v.actor,
        exam_id=serializers.UUIDField().run_validation(r.query_params.get("exam_id") or None),
        **_section(r),
    ),
)
StaffAttendanceReport = _report(
    "staff-attendance",
    "staff_attendance.read",
    "Staff attendance for a period",
    [FROM, TO],
    lambda v, r: builders.staff_attendance(v.actor, **_period(v, r)),
)


def _admissions(v: TenantAPIView, r: Request) -> builders.Table:
    _school_only(v, "admission.read")
    return builders.admissions_funnel(v.actor, **_period(v, r))


AdmissionsReport = _report(
    "admissions", "admission.read", "Admissions funnel by source and stage", [FROM, TO], _admissions
)
