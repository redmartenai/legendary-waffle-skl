"""A member's own notification centre. Every member may read their own notifications (``school.read``)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.pagination import CursorPagination
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import TenantAPIView
from eduflow.core.api import TENANT_HEADER, errors

from .. import services
from ..models import NotificationPreference
from . import serializers as s

TAG = ["notifications"]


class _Page(CursorPagination):
    ordering = "-created_at"
    page_size = 50
    max_page_size = 200
    page_size_query_param = "page_size"


class NotificationListView(TenantAPIView):
    required_permissions = {"GET": "school.read"}

    @extend_schema(
        tags=TAG,
        summary="My notifications",
        description="The caller's own notifications in this school, newest first. `?unread=true` filters.",
        parameters=[TENANT_HEADER, OpenApiParameter("unread", bool)],
        responses={200: s.NotificationOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        rows = services.inbox(self.actor.membership)
        if request.query_params.get("unread") in ("true", "1"):
            rows = rows.filter(read_at__isnull=True)
        paginator = _Page()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(s.NotificationOut(page, many=True).data)


class NotificationReadView(TenantAPIView):
    required_permissions = {"POST": "school.read"}

    @extend_schema(
        tags=TAG,
        summary="Mark my notifications read",
        parameters=[TENANT_HEADER],
        request=s.ReadIn,
        responses={200: None, **errors(400, 401, 403)},
    )
    def post(self, request: Request) -> Response:
        body = s.ReadIn(data=request.data)
        body.is_valid(raise_exception=True)
        count = services.mark_read(self.actor.membership, body.validated_data.get("ids"))
        return Response({"updated": count})


class PreferenceView(TenantAPIView):
    required_permissions = {"GET": "school.read", "PUT": "school.read"}

    def _current(self) -> Response:
        prefs = NotificationPreference.objects.filter(membership=self.actor.membership).order_by(
            "kind", "channel"
        )
        return Response(
            {"preferences": [{"kind": p.kind, "channel": p.channel, "enabled": p.enabled} for p in prefs]}
        )

    @extend_schema(
        tags=TAG,
        summary="My external delivery preferences",
        description="External channels are opt-in per kind. In-app notifications are always on.",
        parameters=[TENANT_HEADER],
        responses={200: s.PreferencesOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return self._current()

    @extend_schema(
        tags=TAG,
        summary="Set my external delivery preferences",
        parameters=[TENANT_HEADER],
        request=s.PreferencesIn,
        responses={200: s.PreferencesOut, **errors(400, 401, 403)},
    )
    def put(self, request: Request) -> Response:
        body = s.PreferencesIn(data=request.data)
        body.is_valid(raise_exception=True)
        services.set_preferences(self.actor.membership, body.validated_data["preferences"])
        return self._current()
