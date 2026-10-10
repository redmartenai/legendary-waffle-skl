"""Monitoring endpoints.

* ``monitoring.read``: alerts (school: all; self: alerts with an item the caller owns, trimmed to those
  items), the pulse (school), student risk (over the students the caller may see) and scorecards (school:
  every teacher; self: their own).
* ``monitoring.manage``: resolve alerts, run an evaluation now, thresholds (school-wide).
* ``monitoring.ask``: Ask EduFlow (answers read through the caller's own permissions).
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
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
from eduflow.people.models import StaffProfile, Student

from .. import ask, engine, insights, policies, rules, services, signals
from ..models import Alert, AlertStatus, Domain, Severity
from . import serializers as s

TAG = "monitoring"
READ, MANAGE = "monitoring.read", "monitoring.manage"


def _school_wide(view: TenantAPIView, permission: str) -> bool:
    return DataScope.SCHOOL in view.actor.scopes(permission)


def _require_school(view: TenantAPIView, permission: str) -> None:
    if not _school_wide(view, permission):
        view.permission_denied(view.request)


def _trimmed(view: TenantAPIView, data: dict[str, Any]) -> dict[str, Any]:
    """Without school scope, show only the caller's own rows of an alert."""
    if _school_wide(view, READ):
        return data
    mine = str(policies.my_staff_id(view.actor))
    return {**data, "items": [i for i in data["items"] if i.get("staff_id") == mine]}


class _Alert(ResourceView):
    tag = TAG
    resource = policies.alerts
    read_permission = READ
    output_serializer = s.AlertOut

    def base_queryset(self) -> QuerySet[Alert]:
        return Alert.objects.all()


@document_resource
class AlertList(_Alert, ResourceListView):
    filters = [
        Filter("status", "status", serializers.ChoiceField(AlertStatus.choices)),
        Filter("domain", "domain", serializers.ChoiceField(Domain.choices)),
        Filter("severity", "severity", serializers.ChoiceField(Severity.choices)),
        Filter("rule", "rule", serializers.CharField()),
    ]

    def get(self, request: Request) -> Response:
        response = super().get(request)
        response.data["results"] = [_trimmed(self, row) for row in response.data["results"]]
        return response


@document_resource
class AlertDetail(_Alert, ResourceDetailView):
    def get(self, request: Request, pk: Any) -> Response:
        return Response(_trimmed(self, dict(s.AlertOut(self.get_object(pk, READ)).data)))


class AlertAcknowledgeView(TenantAPIView):
    required_permissions = {"POST": READ}

    @extend_schema(
        tags=[TAG],
        summary="Acknowledge an alert you can see",
        description="Stops escalation. The alert stays until it is resolved or the rule stops firing.",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.AlertOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        alert = services.acknowledge(self.actor, policies.alerts.get(self.actor, READ, pk))
        return Response(_trimmed(self, dict(s.AlertOut(alert).data)))


class AlertResolveView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Resolve an alert",
        description="If the rule still fires at the next evaluation, a new alert opens.",
        parameters=[TENANT_HEADER],
        request=s.AlertResolveIn,
        responses={200: s.AlertOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _require_school(self, MANAGE)
        body = s.AlertResolveIn(data=request.data)
        body.is_valid(raise_exception=True)
        alert = services.resolve(
            self.actor, policies.alerts.get(self.actor, MANAGE, pk), **body.validated_data
        )
        return Response(s.AlertOut(alert).data)


class EvaluateView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Evaluate every rule now",
        description="The scheduler does this every 15 minutes; use it after changing thresholds.",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.EvaluatedOut, **errors(401, 403)},
    )
    def post(self, request: Request) -> Response:
        _require_school(self, MANAGE)
        return Response(engine.evaluate(self.actor.school))


class RulesView(TenantAPIView):
    required_permissions = {"GET": READ}

    @extend_schema(
        tags=[TAG],
        summary="The rule catalogue",
        parameters=[TENANT_HEADER],
        responses={200: s.RuleOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response(s.RuleOut(list(rules.RULES.values()), many=True).data)


class SettingsView(TenantAPIView):
    required_permissions = {"GET": READ, "PUT": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Monitoring thresholds",
        parameters=[TENANT_HEADER],
        responses={200: s.MonitoringSettingsOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response(s.MonitoringSettingsOut(signals.settings_for(self.actor.school)).data)

    @extend_schema(
        tags=[TAG],
        summary="Change monitoring thresholds",
        description="Defaults are the prototype's: 75% attendance, 12-point drop, 24h reply, 08:15.",
        parameters=[TENANT_HEADER],
        request=s.MonitoringSettingsIn,
        responses={200: s.MonitoringSettingsOut, **errors(400, 401, 403)},
    )
    def put(self, request: Request) -> Response:
        _require_school(self, MANAGE)
        body = s.MonitoringSettingsIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.MonitoringSettingsOut(services.update_settings(self.actor, **body.validated_data)).data
        )


class PulseView(TenantAPIView):
    required_permissions = {"GET": READ}

    @extend_schema(
        tags=[TAG],
        summary="School pulse: today's headline KPIs",
        parameters=[TENANT_HEADER],
        responses={200: s.PulseOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        _require_school(self, READ)
        return Response(s.PulseOut(insights.pulse(self.actor.school)).data)


class RiskView(TenantAPIView):
    required_permissions = {"GET": READ}

    @extend_schema(
        tags=[TAG],
        summary="Student risk, highest first",
        description="Over the students the caller may see under `student.read` and `monitoring.read`.",
        parameters=[
            TENANT_HEADER,
            OpenApiParameter("section_id", OpenApiTypes.UUID),
            OpenApiParameter("level", OpenApiTypes.STR, enum=["ok", "watch", "at_risk"]),
        ],
        responses={200: s.RiskOut(many=True), **errors(400, 401, 403)},
    )
    def get(self, request: Request) -> Response:
        students = people_policies.students.queryset(
            self.actor, READ, Student.objects.filter(status="active")
        )
        students = people_policies.students.queryset(self.actor, "student.read", students)
        section_id = request.query_params.get("section_id")
        if section_id:
            section = serializers.UUIDField().run_validation(section_id)
            students = students.filter(enrollments__section_id=section, enrollments__status="active")
        level = request.query_params.get("level")
        if level:
            serializers.ChoiceField(["ok", "watch", "at_risk"]).run_validation(level)
        rows = [
            r for r in insights.student_risk(self.actor.school, students) if not level or r.level == level
        ]
        return Response(s.RiskOut(rows, many=True).data)


class StudentRiskView(TenantAPIView):
    required_permissions = {"GET": READ}

    @extend_schema(
        tags=[TAG],
        summary="One student's risk and why",
        parameters=[TENANT_HEADER],
        responses={200: s.RiskOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        student = people_policies.students.get(self.actor, READ, pk)
        return Response(s.RiskOut(insights.student_risk(self.actor.school, [student])[0]).data)


class ScorecardsView(TenantAPIView):
    required_permissions = {"GET": READ}

    @extend_schema(
        tags=[TAG],
        summary="Teacher scorecards",
        description="School scope: every teaching staff member. Self scope: your own scorecard.",
        parameters=[
            TENANT_HEADER,
            OpenApiParameter("days", OpenApiTypes.INT, description="Window, default 30."),
        ],
        responses={200: s.ScorecardOut(many=True), **errors(400, 401, 403)},
    )
    def get(self, request: Request) -> Response:
        staff = StaffProfile.objects.filter(school_id=self.actor.school.pk, status="active").select_related(
            "membership__user"
        )
        if _school_wide(self, READ):
            staff = staff.filter(staff_type="teaching")
        else:
            staff = staff.filter(membership=self.actor.membership)
        days = serializers.IntegerField(min_value=7, max_value=365).run_validation(
            request.query_params.get("days") or 30
        )
        return Response(
            s.ScorecardOut(insights.scorecards(self.actor.school, staff, days=days), many=True).data
        )


class AskView(TenantAPIView):
    required_permissions = {"GET": "monitoring.ask", "POST": "monitoring.ask"}

    @extend_schema(
        tags=[TAG],
        summary="Suggested questions",
        parameters=[TENANT_HEADER],
        responses={200: s.SuggestionsOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response({"suggestions": ask.SUGGESTIONS})

    @extend_schema(
        tags=[TAG],
        summary="Ask EduFlow",
        description=(
            "Plain-language questions answered from data you may see (deterministic intents; no AI). "
            "An intent you have no permission for says so without revealing data."
        ),
        parameters=[TENANT_HEADER],
        request=s.AskIn,
        responses={200: s.AnswerOut, **errors(400, 401, 403)},
    )
    def post(self, request: Request) -> Response:
        body = s.AskIn(data=request.data)
        body.is_valid(raise_exception=True)
        answer = ask.ask(self.actor, body.validated_data["question"])
        from eduflow.tenancy import domain

        domain.record("monitoring.ask.asked", self.actor.school, intent=answer.intent, rows=len(answer.rows))
        return Response(s.AnswerOut(answer).data)
