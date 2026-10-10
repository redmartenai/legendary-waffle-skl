"""The central approvals queue. Listing needs ``approval.read``; each kind needs its own decide permission
(school-wide), checked by the registry and again by the module's service."""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors

from ..registry import DECISIONS, pending_for, registry
from . import serializers as s

TAG = ["approvals"]


class ApprovalQueueView(TenantAPIView):
    required_permissions = {"GET": "approval.read"}

    @extend_schema(
        tags=TAG,
        summary="Pending approvals I can decide",
        description=(
            "Every pending request, across modules, whose kind the caller may decide school-wide "
            "(attendance corrections, leave, refunds, admissions, marks corrections, outpasses, purchases, "
            "visitors), oldest first."
        ),
        parameters=[TENANT_HEADER],
        responses={200: s.ApprovalItemOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        now = timezone.now()
        items = [
            {
                **item.__dict__,
                "id": str(item.id),
                "waiting_hours": int((now - item.requested_at).total_seconds() // 3600),
            }
            for item in pending_for(self.actor)
        ]
        return Response(s.ApprovalItemOut(items, many=True).data)


class ApprovalDecisionView(TenantAPIView):
    required_permissions = {"POST": "approval.read"}

    @extend_schema(
        tags=TAG,
        summary="Decide a pending approval",
        description=(
            "Delegates to the owning module, which applies its own rules (state, permission, conflicts) "
            "and audit. Unknown kinds and requests outside the caller's school are `404`."
        ),
        parameters=[TENANT_HEADER],
        request=s.ApprovalDecisionIn,
        responses={200: None, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, kind: str, pk: Any) -> Response:
        provider = registry.providers.get(kind)
        if provider is None:
            raise NotFound()
        if DataScope.SCHOOL not in self.actor.scopes(provider.permission):
            self.permission_denied(request)
        body = s.ApprovalDecisionIn(data=request.data)
        body.is_valid(raise_exception=True)
        provider.decide(
            self.actor, pk, DECISIONS[body.validated_data["decision"]], body.validated_data.get("note", "")
        )
        return Response({"kind": kind, "id": str(pk), "decision": DECISIONS[body.validated_data["decision"]]})
