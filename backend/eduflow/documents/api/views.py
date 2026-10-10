"""Documents: list, upload (multipart), read, archive, download. Reads and downloads follow the caller's data
scope and the document's audience list (policies.py); downloads are audited."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.parsers import MultiPartParser
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
from ..models import Document
from . import serializers as s

TAG = "documents"


class _Document(ResourceView):
    tag = TAG
    resource = policies.documents
    read_permission, write_permission = "document.read", "document.manage"
    output_serializer = s.DocumentOut

    def base_queryset(self) -> QuerySet[Document]:
        return Document.objects.select_related("file").filter(archived_at__isnull=True)


@document_resource
class DocumentList(_Document, ResourceListView):
    create_serializer = s.DocumentIn
    parser_classes = [MultiPartParser]
    filters = [
        Filter("student_id", "student_id", serializers.UUIDField()),
        Filter("staff_id", "staff_id", serializers.UUIDField()),
        Filter("category", "category", serializers.CharField()),
    ]

    def post(self, request: Request) -> Response:
        # A teacher may upload about students they teach (re-checked in the service); others: school scope.
        body = s.DocumentIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        document = services.upload(self.actor, upload_file=data.pop("file"), **data)
        return Response(s.DocumentOut(document).data, status=201)


@document_resource
class DocumentDetail(_Document, ResourceDetailView):
    allow_delete = True

    def perform_delete(self, obj: Document) -> None:
        services.archive(self.actor, obj)


class DocumentDownloadView(TenantAPIView):
    required_permissions = {"GET": "document.download"}

    @extend_schema(
        tags=[TAG],
        summary="Download a document",
        description="Requires `document.download` over the document (scope and audience). Audited.",
        parameters=[TENANT_HEADER],
        responses={
            (200, "application/octet-stream"): OpenApiResponse(OpenApiTypes.BINARY),
            **errors(401, 403, 404),
        },
    )
    def get(self, request: Request, pk: Any) -> HttpResponse:
        base = Document.objects.select_related("file").filter(archived_at__isnull=True)
        document = policies.documents.get(self.actor, "document.download", pk, base=base)
        return services.download(self.actor, document)
