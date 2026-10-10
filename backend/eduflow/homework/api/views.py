"""Homework endpoints. Reads follow the section (homework) or the student (submissions); writes are narrow:
a teacher sets and reviews homework for a section and subject they teach (re-checked in the services)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.parsers import JSONParser, MultiPartParser
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
from eduflow.documents import files
from eduflow.people import policies as people_policies

from .. import policies, services
from ..models import Homework, HomeworkStatus, Submission
from . import serializers as s

TAG = "homework"
BINARY: dict[Any, Any] = {
    (200, "application/octet-stream"): OpenApiResponse(OpenApiTypes.BINARY),
    **errors(401, 403, 404),
}


class _Homework(ResourceView):
    tag = TAG
    resource = policies.homework
    read_permission, write_permission = "homework.read", "homework.manage"
    output_serializer = s.HomeworkOut
    narrow_writes = True

    def base_queryset(self) -> QuerySet[Homework]:
        return Homework.objects.select_related(
            "section", "subject", "attachment", "teacher__membership__user"
        )


@document_resource
class HomeworkList(_Homework, ResourceListView):
    create_serializer = s.HomeworkIn
    parser_classes = [JSONParser, MultiPartParser]
    filters = [
        Filter("section_id", "section_id", serializers.UUIDField()),
        Filter("subject_id", "subject_id", serializers.UUIDField()),
        Filter("status", "status", serializers.ChoiceField(HomeworkStatus.choices)),
        Filter("due_from", "due_date__gte", serializers.DateField()),
        Filter("due_to", "due_date__lte", serializers.DateField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Homework:
        return services.create(self.actor, **data)


@document_resource
class HomeworkDetail(_Homework, ResourceDetailView):
    update_serializer = s.HomeworkUpdateIn
    allow_delete = True

    def perform_update(self, obj: Homework, data: dict[str, Any]) -> Homework:
        return services.update(self.actor, obj, **data)

    def perform_delete(self, obj: Homework) -> None:
        services.archive(self.actor, obj)


def _homework(view: TenantAPIView, pk: Any) -> Homework:
    return policies.homework.get(view.actor, "homework.read", pk, base=_Homework.base_queryset(view))  # type: ignore[arg-type]


class HomeworkBoardView(TenantAPIView):
    required_permissions = {"GET": "homework.read"}

    @extend_schema(
        tags=[TAG],
        summary="Submission status of every student the caller may see",
        description="One row per actively enrolled student of the section; no submission means `pending`.",
        parameters=[TENANT_HEADER],
        responses={200: s.BoardRowOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        homework = _homework(self, pk)
        students = people_policies.students.queryset(
            self.actor, "homework.read", base=services.roster(homework)
        )
        rows = services.status_board(homework, students)
        return Response(s.BoardRowOut(rows, many=True).data)


class HomeworkSubmitView(TenantAPIView):
    required_permissions = {"POST": "homework.submit"}
    parser_classes = [JSONParser, MultiPartParser]

    @extend_schema(
        tags=[TAG],
        summary="Submit homework (the signed-in student)",
        description="After the due date (school time) the submission is `late`. Replaceable until reviewed.",
        parameters=[TENANT_HEADER],
        request={"multipart/form-data": s.HomeworkSubmitIn, "application/json": s.HomeworkSubmitIn},
        responses={200: s.SubmissionOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        homework = _homework(self, pk)
        body = s.HomeworkSubmitIn(data=request.data)
        body.is_valid(raise_exception=True)
        sub = services.submit(
            self.actor,
            homework,
            text=body.validated_data.get("text", ""),
            upload=body.validated_data.get("file"),
        )
        return Response(s.SubmissionOut(sub).data)


def _submission(view: TenantAPIView, pk: Any, permission: str) -> Submission:
    base = Submission.objects.select_related("student", "file", "homework__section", "homework__subject")
    return policies.submissions.get(view.actor, permission, pk, base=base)


class SubmissionReviewView(TenantAPIView):
    required_permissions = {"POST": "homework.review"}

    @extend_schema(
        tags=[TAG],
        summary="Review a submission",
        parameters=[TENANT_HEADER],
        request=s.ReviewIn,
        responses={200: s.SubmissionOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        sub = _submission(self, pk, "homework.review")
        body = s.ReviewIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(s.SubmissionOut(services.review(self.actor, sub, **body.validated_data)).data)


class HomeworkAttachmentView(TenantAPIView):
    required_permissions = {"GET": "homework.read"}

    @extend_schema(
        tags=[TAG], summary="Download the homework attachment", parameters=[TENANT_HEADER], responses=BINARY
    )
    def get(self, request: Request, pk: Any) -> HttpResponse:
        homework = _homework(self, pk)
        if homework.attachment is None:
            raise NotFound()
        return files.download(homework.attachment)


class SubmissionFileView(TenantAPIView):
    required_permissions = {"GET": "homework.read"}

    @extend_schema(
        tags=[TAG], summary="Download a submitted file", parameters=[TENANT_HEADER], responses=BINARY
    )
    def get(self, request: Request, pk: Any) -> HttpResponse:
        sub = _submission(self, pk, "homework.read")
        if sub.file is None:
            raise NotFound()
        return files.download(sub.file)
