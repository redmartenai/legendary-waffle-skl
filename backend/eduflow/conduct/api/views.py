"""Remarks and behaviour incidents. Reads follow the student (families: only what is visible to them);
writes are narrow: a teacher writes about students they teach (re-checked in the services)."""

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
from eduflow.core.api import TENANT_HEADER, errors

from .. import policies, services
from ..models import Incident, IncidentStatus, Remark, Severity, Tone
from . import serializers as s

TAG = "conduct"


class _Remark(ResourceView):
    tag = TAG
    resource = policies.remarks
    read_permission, write_permission = "remark.read", "remark.manage"
    output_serializer = s.RemarkOut
    narrow_writes = True

    def base_queryset(self) -> QuerySet[Remark]:
        return Remark.objects.select_related("student", "subject", "author__user")


@document_resource
class RemarkList(_Remark, ResourceListView):
    create_serializer = s.RemarkIn
    filters = [
        Filter("student_id", "student_id", serializers.UUIDField()),
        Filter("tone", "tone", serializers.ChoiceField(Tone.choices)),
    ]

    def perform_create(self, data: dict[str, Any]) -> Remark:
        return services.add_remark(self.actor, **data)


@document_resource
class RemarkDetail(_Remark, ResourceDetailView):
    update_serializer = s.RemarkUpdateIn
    allow_delete = True

    def perform_update(self, obj: Remark, data: dict[str, Any]) -> Remark:
        return services.update_remark(self.actor, obj, **data)

    def perform_delete(self, obj: Remark) -> None:
        services.delete_remark(self.actor, obj)


class _Incident(ResourceView):
    tag = TAG
    resource = policies.incidents
    read_permission, write_permission = "behaviour.read", "behaviour.manage"
    output_serializer = s.IncidentOut
    narrow_writes = True

    def base_queryset(self) -> QuerySet[Incident]:
        return Incident.objects.select_related("student", "reported_by__user")


@document_resource
class IncidentList(_Incident, ResourceListView):
    create_serializer = s.IncidentIn
    filters = [
        Filter("student_id", "student_id", serializers.UUIDField()),
        Filter("severity", "severity", serializers.ChoiceField(Severity.choices)),
        Filter("status", "status", serializers.ChoiceField(IncidentStatus.choices)),
        Filter("from", "occurred_on__gte", serializers.DateField()),
        Filter("to", "occurred_on__lte", serializers.DateField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Incident:
        return services.report_incident(self.actor, **data)


@document_resource
class IncidentDetail(_Incident, ResourceDetailView):
    update_serializer = s.IncidentUpdateIn

    def perform_update(self, obj: Incident, data: dict[str, Any]) -> Incident:
        return services.update_incident(self.actor, obj, **data)


class IncidentResolveView(TenantAPIView):
    required_permissions = {"POST": "behaviour.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Resolve a behaviour incident",
        parameters=[TENANT_HEADER],
        request=s.IncidentResolveIn,
        responses={200: s.IncidentOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        incident = policies.incidents.get(self.actor, "behaviour.manage", pk)
        body = s.IncidentResolveIn(data=request.data)
        body.is_valid(raise_exception=True)
        services.resolve_incident(self.actor, incident, **body.validated_data)
        fresh = Incident.objects.select_related("student", "reported_by__user").get(pk=incident.pk)
        return Response(s.IncidentOut(fresh).data)
