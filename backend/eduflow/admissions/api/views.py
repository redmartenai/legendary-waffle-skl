"""Admissions endpoints. Staff endpoints need ``admission.read`` / ``admission.manage`` school-wide;
offers are decided in the approvals queue (``admission.approve``). The public apply endpoint is
rate-limited per IP and only ever creates an enquiry; it returns a reference, never data."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

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
from eduflow.identity.throttles import IpThrottle
from eduflow.tenancy.selectors import active_school_by_code

from .. import policies, services
from ..models import Application, ApplicationDocument, Source, Stage
from . import serializers as s

TAG = "admissions"


class OnlineApplicationIpThrottle(IpThrottle):
    scope = "admission_apply_ip"


class _Application(ResourceView):
    tag = TAG
    resource = policies.applications
    read_permission, write_permission = "admission.read", "admission.manage"
    output_serializer = s.ApplicationOut

    def base_queryset(self) -> QuerySet[Application]:
        return Application.objects.select_related("grade", "academic_year").prefetch_related(
            "history__by__user"
        )


@document_resource
class ApplicationList(_Application, ResourceListView):
    create_serializer = s.ApplicationIn
    filters = [
        Filter("stage", "stage", serializers.ChoiceField(Stage.choices)),
        Filter("source", "source", serializers.ChoiceField(Source.choices)),
        Filter("grade_id", "grade_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Application:
        return services.create(self.actor, **data)


@document_resource
class ApplicationDetail(_Application, ResourceDetailView):
    update_serializer = s.ApplicationUpdateIn

    def perform_update(self, obj: Application, data: dict[str, Any]) -> Application:
        return services.update(self.actor, obj, **data)


class _Action(TenantAPIView):
    required_permissions = {"POST": "admission.manage"}

    def load(self, pk: Any) -> Application:
        if DataScope.SCHOOL not in self.actor.scopes("admission.manage"):
            self.permission_denied(self.request)
        return policies.applications.get(self.actor, "admission.manage", pk)

    def respond(self, app: Application, status: int = 200) -> Response:
        fresh = _Application.base_queryset(self).get(pk=app.pk)  # type: ignore[arg-type]
        return Response(s.ApplicationOut(fresh).data, status=status)


class ApplicationMoveView(_Action):
    @extend_schema(
        tags=[TAG],
        summary="Move an application to a later stage, or drop it",
        description="Forwards only. Moving to `offer` puts it in the approvals queue.",
        parameters=[TENANT_HEADER],
        request=s.MoveIn,
        responses={200: s.ApplicationOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        app = self.load(pk)
        body = s.MoveIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.move(self.actor, app, **body.validated_data))


class ApplicationEnrolView(_Action):
    @extend_schema(
        tags=[TAG],
        summary="Enrol an approved offer",
        description=(
            "Creates the student, the guardian, their link and the enrollment in the given section of the "
            "applied-for grade. Needs `student.create`, `guardian.manage` and `enrollment.manage` "
            "school-wide."
        ),
        parameters=[TENANT_HEADER],
        request=s.EnrolIn,
        responses={200: s.ApplicationOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        app = self.load(pk)
        body = s.EnrolIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.enrol(self.actor, app, **body.validated_data))


class ApplicationDocumentListView(TenantAPIView):
    required_permissions = {"GET": "admission.read", "POST": "admission.manage"}
    parser_classes = [MultiPartParser]

    def _app(self, pk: Any, permission: str) -> Application:
        if DataScope.SCHOOL not in self.actor.scopes(permission):
            self.permission_denied(self.request)
        return policies.applications.get(self.actor, permission, pk)

    @extend_schema(
        tags=[TAG],
        summary="An application's documents",
        parameters=[TENANT_HEADER],
        responses={200: s.AppDocumentOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        app = self._app(pk, "admission.read")
        return Response(s.AppDocumentOut(app.documents.select_related("file"), many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Attach a document to an application",
        parameters=[TENANT_HEADER],
        request={"multipart/form-data": s.AppDocumentIn},
        responses={201: s.AppDocumentOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        app = self._app(pk, "admission.manage")
        body = s.AppDocumentIn(data=request.data)
        body.is_valid(raise_exception=True)
        doc = services.attach(
            self.actor, app, upload=body.validated_data["file"], title=body.validated_data["title"]
        )
        return Response(s.AppDocumentOut(doc).data, status=201)


class ApplicationDocumentDownloadView(TenantAPIView):
    required_permissions = {"GET": "admission.read"}

    @extend_schema(
        tags=[TAG],
        summary="Download an application document",
        parameters=[TENANT_HEADER],
        responses={
            (200, "application/octet-stream"): OpenApiResponse(OpenApiTypes.BINARY),
            **errors(401, 403, 404),
        },
    )
    def get(self, request: Request, pk: Any, document_id: Any) -> HttpResponse:
        if DataScope.SCHOOL not in self.actor.scopes("admission.read"):
            self.permission_denied(request)
        app = policies.applications.get(self.actor, "admission.read", pk)
        doc = (
            ApplicationDocument.objects.select_related("file").filter(application=app, pk=document_id).first()
        )
        if doc is None:
            raise NotFound()
        from eduflow.tenancy import domain

        domain.record("admissions.application.document_downloaded", app, document=str(doc.pk))
        return files.download(doc.file)


class OnlineApplicationView(APIView):
    """Public: a family applies online. Creates an enquiry; returns a reference only."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [OnlineApplicationIpThrottle]

    @extend_schema(
        tags=[TAG],
        summary="Apply online (public)",
        description="Creates an enquiry with source `website` in the school with that code. Rate-limited.",
        request=s.OnlineApplicationIn,
        responses={201: s.OnlineReceiptOut, **errors(400, 404, 429)},
    )
    def post(self, request: Request) -> Response:
        body = s.OnlineApplicationIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        school = active_school_by_code(data.pop("school_code"))
        if school is None:
            raise NotFound()
        from eduflow.core import db_context

        message = data.pop("message", "")
        with db_context.scoped(db_context.DbContext(school_id=school.pk)):
            app = services.apply_online(school, notes=message, **data)
        return Response(
            {"reference": app.pk, "message": "Thank you. The school will contact you about the next steps."},
            status=201,
        )
