"""Attendance endpoints. Thin: authorization in the base views and scope rules, rules in the services.

Client paths (CURRENT_STATE §5, ADR-006):

* ``GET /classes/{id}/roster?date=`` and ``POST /classes/{id}/attendance``: the section must be in the
  caller's ``attendance.create`` scope, and the service requires an active assignment in it unless the
  grant is school-wide (ADR-027).
* ``GET /students/{id}/attendance?month=``: the student must be visible under both ``student.read`` and
  ``attendance.read`` (the subject rule of ADR-027): a parent sees their children, a student themselves, a
  teacher the students of the sections they teach.

Canonical resources (read-only except corrections): ``/attendance/sessions``, ``/attendance/records`` and
``/attendance/corrections`` with ``/approve`` and ``/decline``.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import Count, Q, QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.academics.models import Section
from eduflow.academics.policies import sections
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

from .. import policies, selectors, services
from ..models import AttendanceCorrection, AttendanceRecord, AttendanceSession, AttendanceStatus
from . import serializers as s

TAG = "attendance"
UUID = serializers.UUIDField


def _section(actor: Any, permission: str, pk: Any) -> Section:
    base = Section.objects.select_related("academic_year", "grade")
    return sections.get(actor, permission, pk, base=base)


def _summary(session: AttendanceSession, replayed: bool) -> dict[str, Any]:
    statuses = list(session.records.values_list("status", flat=True))
    return {
        "session_id": session.pk,
        "klass": s.class_ref(session.section),
        "date": session.date,
        "marked_at": session.submitted_at,
        "cutoff": session.locked_at,
        "locked": services.is_locked(session),
        "total": len(statuses),
        "counts": selectors.counts(statuses),
        "replayed": replayed,
    }


def _initials(student: Any) -> str:
    parts = [student.first_name, student.last_name]
    return "".join(p[0] for p in parts if p).upper()


# ------------------------------------------------------------------------------------------- class register
class ClassRosterView(TenantAPIView):
    required_permissions = {"GET": "attendance.create"}

    @extend_schema(
        tags=[TAG],
        summary="A class roster for taking attendance",
        description=(
            "Requires `attendance.create` over the section; a teacher must hold an active assignment in it. "
            "Students are those enrolled in the section on that date (a student transferred that day "
            "appears in the new section only)."
        ),
        parameters=[TENANT_HEADER, OpenApiParameter("date", datetime.date, description="Defaults to today.")],
        responses={200: s.ClassRosterOut, **errors(400, 401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        section = _section(self.actor, "attendance.create", pk)
        services.can_take(self.actor, section)
        query = s.RosterQuery(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or services.school_today(self.actor)
        services.check_register_date(self.actor, section, day)
        session = selectors.session_for(section, day)
        marked = {r.student_id: r.status for r in session.records.all()} if session else {}
        students = [
            {
                "id": e.student_id,
                "name": e.student.full_name,
                "initials": _initials(e.student),
                "roll_no": e.roll_number,
                "status": marked.get(e.student_id),
            }
            for e in selectors.roster(section, day)
        ]
        payload = {
            "klass": s.class_ref(section),
            "date": day,
            "marked": session is not None,
            "marked_at": session.submitted_at if session else None,
            "cutoff": session.locked_at if session else services.lock_moment(self.actor, day),
            "locked": services.is_locked(session) if session else False,
            "students": students,
        }
        return Response(s.ClassRosterOut(payload).data)


class ClassAttendanceView(TenantAPIView):
    required_permissions = {"POST": "attendance.create"}

    @extend_schema(
        tags=[TAG],
        summary="Take a class register",
        description=(
            "Requires `attendance.create` over the section; a teacher must hold an active assignment in it. "
            "Send the exceptions only: every student on the roster who is not listed is `present` (a full "
            "replacement, ADR-008). Re-sending the same `client_id` returns the stored register unchanged. "
            "Until the end of the date (school time) the register can be submitted again; after that, "
            "`409` (request a correction)."
        ),
        parameters=[TENANT_HEADER],
        request=s.SubmitIn,
        responses={200: s.AttendanceSummaryOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        section = _section(self.actor, "attendance.create", pk)
        body = s.SubmitIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        client_id = data.pop("client_id", "") or request.headers.get("Idempotency-Key", "")
        session, replayed = services.submit_register(self.actor, section, client_id=client_id[:64], **data)
        session = AttendanceSession.objects.select_related("section__grade").get(pk=session.pk)
        return Response(s.AttendanceSummaryOut(_summary(session, replayed)).data)


# -------------------------------------------------------------------------------------------- student month
class StudentAttendanceView(TenantAPIView):
    required_permissions = {"GET": "attendance.read"}

    @extend_schema(
        tags=[TAG],
        summary="A student's attendance for a month",
        description=(
            "Requires `attendance.read` and `student.read`, both covering the student. Each day shows the "
            "status in the section the student belonged to that day; `not_marked` when no register has it, "
            "`upcoming` after today, and null on days the student was not enrolled. Holidays are not known "
            "yet (no school calendar)."
        ),
        parameters=[
            TENANT_HEADER,
            OpenApiParameter("month", str, description="YYYY-MM; defaults to this month."),
        ],
        responses={200: s.AttendanceMonthOut, **errors(400, 401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        student = people_policies.students.get(self.actor, "student.read", pk)
        if (
            not people_policies.students.queryset(self.actor, "attendance.read")
            .filter(pk=student.pk)
            .exists()
        ):
            raise NotFound()
        query = s.MonthQuery(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        today = services.school_today(self.actor)
        month = query.validated_data.get("month") or today.strftime("%Y-%m")
        first, last = selectors.month_days(month)
        days = selectors.student_month(student, first, last, today)
        payload = {
            "month": month,
            "days": [{"date": d.date, "status": d.status} for d in days],
            "summary": selectors.month_summary(days),
        }
        return Response(s.AttendanceMonthOut(payload).data)


# ------------------------------------------------------------------------------------------------ registers
def _with_counts(qs: QuerySet[AttendanceSession]) -> QuerySet[AttendanceSession]:
    return qs.annotate(
        **{f"n_{st}": Count("records", filter=Q(records__status=st)) for st in AttendanceStatus.values}
    )


class _Session(ResourceView):
    tag = TAG
    resource = policies.sessions
    read_permission = "attendance.read"
    output_serializer = s.RegisterOut

    def base_queryset(self) -> QuerySet[AttendanceSession]:
        return _with_counts(AttendanceSession.objects.select_related("section", "taken_by__user"))


@document_resource
class SessionList(_Session, ResourceListView):
    filters = [
        Filter("section_id", "section_id", UUID()),
        Filter("academic_year_id", "academic_year_id", UUID()),
        Filter("date", "date", serializers.DateField()),
        Filter("date_from", "date__gte", serializers.DateField()),
        Filter("date_to", "date__lte", serializers.DateField()),
    ]


@document_resource
class SessionDetail(_Session, ResourceDetailView):
    pass


# ------------------------------------------------------------------------------------------------ records
class _Record(ResourceView):
    tag = TAG
    resource = policies.records
    read_permission = "attendance.read"
    output_serializer = s.RecordOut

    def base_queryset(self) -> QuerySet[AttendanceRecord]:
        return AttendanceRecord.objects.select_related("student", "section")


@document_resource
class RecordList(_Record, ResourceListView):
    filters = [
        Filter("student_id", "student_id", UUID()),
        Filter("section_id", "section_id", UUID()),
        Filter("session_id", "session_id", UUID()),
        Filter("status", "status", s.ATTENDANCE_STATUS),
        Filter("date", "date", serializers.DateField()),
        Filter("date_from", "date__gte", serializers.DateField()),
        Filter("date_to", "date__lte", serializers.DateField()),
    ]


@document_resource
class RecordDetail(_Record, ResourceDetailView):
    pass


# ------------------------------------------------------------------------------------------------ corrections
CORRECTION_RELATED = (
    "record__student",
    "record__section",
    "requested_by__user",
    "decided_by__user",
)


class _Correction(ResourceView):
    tag = TAG
    resource = policies.corrections
    read_permission, write_permission = "attendance.read", "attendance.update"
    output_serializer = s.CorrectionOut

    def base_queryset(self) -> QuerySet[AttendanceCorrection]:
        return AttendanceCorrection.objects.select_related(*CORRECTION_RELATED)


@document_resource
class CorrectionList(_Correction, ResourceListView):
    create_serializer = s.CorrectionIn
    filters = [
        Filter("status", "status", s.CORRECTION_STATUS),
        Filter("record_id", "record_id", UUID()),
        Filter("student_id", "record__student_id", UUID()),
        Filter("section_id", "record__section_id", UUID()),
    ]

    def post(self, request: Request) -> Response:
        # A teacher requests corrections for their own classes (ADR-027): the record is loaded through the
        # caller's attendance.update scope, and the service re-checks the assignment.
        body = s.CorrectionIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        record = policies.records.get(self.actor, "attendance.update", data.pop("record_id"))
        return Response(self.out(services.request_correction(self.actor, record, **data)), status=201)


@document_resource
class CorrectionDetail(_Correction, ResourceDetailView):
    pass


class _Decision(TenantAPIView):
    required_permissions = {"POST": "attendance.approve"}

    def decide(self, request: Request, pk: Any, decide: Any) -> Response:
        if DataScope.SCHOOL not in self.actor.scopes("attendance.approve"):
            self.permission_denied(request)
        correction = policies.corrections.get(self.actor, "attendance.approve", pk)
        body = s.DecisionIn(data=request.data)
        body.is_valid(raise_exception=True)
        decided = decide(self.actor, correction, **body.validated_data)
        fresh = AttendanceCorrection.objects.select_related(*CORRECTION_RELATED).get(pk=decided.pk)
        return Response(s.CorrectionOut(fresh).data)


class CorrectionApproveView(_Decision):
    @extend_schema(
        tags=[TAG],
        summary="Approve an attendance correction",
        description=(
            "Requires `attendance.approve` school-wide, and the approver must not be the requester. The "
            "record takes the new status; `409` if the record changed since the request."
        ),
        parameters=[TENANT_HEADER],
        request=s.DecisionIn,
        responses={200: s.CorrectionOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.decide(request, pk, services.approve_correction)


class CorrectionDeclineView(_Decision):
    @extend_schema(
        tags=[TAG],
        summary="Decline an attendance correction",
        description="Requires `attendance.approve` school-wide. The record is unchanged.",
        parameters=[TENANT_HEADER],
        request=s.DecisionIn,
        responses={200: s.CorrectionOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.decide(request, pk, services.decline_correction)
