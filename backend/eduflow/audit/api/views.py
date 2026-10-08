"""Reading the current school's audit trail. Writing happens only in services."""

from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.pagination import CursorPagination
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import TenantAPIView
from eduflow.core.api import TENANT_HEADER, errors

from ..models import AuditEvent


class AuditEventOut(serializers.ModelSerializer[AuditEvent]):
    class Meta:
        model = AuditEvent
        fields = (
            "id",
            "occurred_at",
            "actor_id",
            "action",
            "outcome",
            "target_type",
            "target_id",
            "request_id",
            "ip",
            "metadata",
        )
        read_only_fields = fields


class AuditCursor(CursorPagination):
    ordering = "-id"  # UUIDv7: newest first
    page_size = 50
    max_page_size = 200
    page_size_query_param = "page_size"


class AuditEventListView(TenantAPIView):
    required_permissions = {"GET": "audit.read"}

    @extend_schema(
        tags=["audit"],
        summary="The school's audit trail (newest first, cursor-paginated)",
        parameters=[
            TENANT_HEADER,
            OpenApiParameter(
                "action", OpenApiTypes.STR, description="Exact action, e.g. `authz.role.updated`."
            ),
            OpenApiParameter("actor_id", OpenApiTypes.UUID),
            OpenApiParameter("cursor", OpenApiTypes.STR),
            OpenApiParameter("page_size", OpenApiTypes.INT),
        ],
        responses={200: AuditEventOut(many=True), **errors(400, 401, 403)},
    )
    def get(self, request: Request) -> Response:
        qs = AuditEvent.objects.filter(school_id=self.actor.school.pk)
        if action := request.query_params.get("action"):
            qs = qs.filter(action=action[:64])
        if actor_id := request.query_params.get("actor_id"):
            qs = qs.filter(actor_id=serializers.UUIDField().run_validation(actor_id))
        paginator = AuditCursor()
        page: Any = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(AuditEventOut(page, many=True).data)
