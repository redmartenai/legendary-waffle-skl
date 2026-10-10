"""Visitor endpoints. ``visitor.register`` (hosts pre-register their visitors; security registers at the
gate), ``visitor.read`` (own visits, or all for security) and ``visitor.manage`` (security: approve, scan)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.api.resources import (
    PAGINATION_PARAMETERS,
    Filter,
    ResourceDetailView,
    ResourceListView,
    ResourceView,
    document_resource,
    paginated,
)
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors

from .. import policies, services
from ..models import Visit, VisitStatus
from . import serializers as s

TAG = "visitors"
MANAGE = services.MANAGE


def _security(view: TenantAPIView) -> None:
    if DataScope.SCHOOL not in view.actor.scopes(MANAGE):
        view.permission_denied(view.request)


def _base() -> QuerySet[Visit]:
    return Visit.objects.select_related("host__user", "student")


def _with_pass(visit: Visit, token: str | None) -> dict[str, Any]:
    return {"visit": s.VisitOut(_base().get(pk=visit.pk)).data, "pass_token": token}


class _Visit(ResourceView):
    tag = TAG
    resource = policies.visits
    read_permission = "visitor.read"
    output_serializer = s.VisitOut

    def base_queryset(self) -> QuerySet[Visit]:
        return _base()


class VisitList(_Visit, ResourceListView):
    write_permission = "visitor.register"
    create_serializer = s.VisitIn
    narrow_writes = True  # hosts register their own visitors; the service decides what each may do
    filters = [
        Filter("expected_on", "expected_on", serializers.DateField()),
        Filter("status", "status", serializers.ChoiceField(VisitStatus.choices)),
    ]

    @extend_schema(
        tags=[TAG],
        operation_id="visit_list",
        summary="List visits",
        description="Requires `visitor.read`: security sees every visit, others the visits they host.",
        parameters=[TENANT_HEADER, *(f.openapi() for f in filters), *PAGINATION_PARAMETERS],
        responses={200: paginated(s.VisitOut), **errors(400, 401, 403)},
    )
    def get(self, request: Request) -> Response:
        return super().get(request)

    @extend_schema(
        tags=[TAG],
        operation_id="visit_create",
        summary="Register a visit",
        description=(
            "Security (`visitor.manage` school-wide) registers at the gate: approved at once, with a pass. "
            "Anyone else pre-registers a visitor they host; security approves it."
        ),
        parameters=[TENANT_HEADER],
        request=s.VisitIn,
        responses={201: s.VisitWithPassOut, **errors(400, 401, 403)},
    )
    def post(self, request: Request) -> Response:
        body = s.VisitIn(data=request.data)
        body.is_valid(raise_exception=True)
        visit, token = services.register(self.actor, **body.validated_data)
        return Response(_with_pass(visit, token), status=201)


@document_resource
class VisitDetail(_Visit, ResourceDetailView):
    pass


class VisitDecisionView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Security approves or declines a pre-registered visit",
        parameters=[TENANT_HEADER],
        request=s.VisitDecisionIn,
        responses={200: s.VisitWithPassOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _security(self)
        visit = policies.visits.get(self.actor, MANAGE, pk)
        body = s.VisitDecisionIn(data=request.data)
        body.is_valid(raise_exception=True)
        visit, token = services.decide(self.actor, visit, **body.validated_data)
        return Response(_with_pass(visit, token))


class VisitPassView(TenantAPIView):
    required_permissions = {"POST": "visitor.register"}

    @extend_schema(
        tags=[TAG],
        summary="Issue a new pass for an approved visit (the old one stops working)",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.PassOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        visit = policies.visits.get(self.actor, "visitor.read", pk)
        return Response({"pass_token": services.reissue_pass(self.actor, visit)})


class VisitCancelView(TenantAPIView):
    required_permissions = {"POST": "visitor.register"}

    @extend_schema(
        tags=[TAG],
        summary="Cancel a visit",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.VisitOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        visit = policies.visits.get(self.actor, "visitor.read", pk)
        services.cancel(self.actor, visit)
        return Response(s.VisitOut(_base().get(pk=visit.pk)).data)


class VisitScanView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Scan a pass at the gate (check in, then check out)",
        parameters=[TENANT_HEADER],
        request=s.ScanIn,
        responses={200: s.VisitOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request) -> Response:
        _security(self)
        body = s.ScanIn(data=request.data)
        body.is_valid(raise_exception=True)
        visit = services.scan(self.actor, token=body.validated_data["token"])
        return Response(s.VisitOut(_base().get(pk=visit.pk)).data)


class VisitInsideView(TenantAPIView):
    required_permissions = {"GET": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Visitors inside the campus now",
        parameters=[TENANT_HEADER],
        responses={200: s.VisitOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        _security(self)
        return Response(s.VisitOut(services.inside_now(self.actor.school), many=True).data)
