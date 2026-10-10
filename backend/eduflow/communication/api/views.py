"""Communication endpoints.

* Announcements: ``announcement.read`` (what is addressed to the caller; the office sees all) and
  ``announcement.manage`` (the office school-wide; a teacher for a section they teach).
* Threads: ``message.read`` / ``message.send``; only participants see a thread (the office school-wide).
* Complaints: ``complaint.create`` (parents for their children), ``complaint.read``, ``complaint.manage``.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone
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

from .. import policies, services
from ..models import Announcement, Complaint, ComplaintCategory, ComplaintStatus, Sentiment, Thread
from . import serializers as s

TAG = "communication"


# ---------------------------------------------------------------------------------------------- announcements
class _Announcement(ResourceView):
    tag = TAG
    resource = policies.announcements
    read_permission, write_permission = "announcement.read", "announcement.manage"
    output_serializer = s.AnnouncementOut
    narrow_writes = True  # teachers announce to sections they teach (checked in the service)

    def base_queryset(self) -> QuerySet[Announcement]:
        return Announcement.objects.select_related("section", "author__user").prefetch_related(
            "responses__member__user"
        )


@document_resource
class AnnouncementList(_Announcement, ResourceListView):
    create_serializer = s.AnnouncementIn
    filters = [
        Filter("section_id", "section_id", serializers.UUIDField()),
        Filter("pinned", "pinned", serializers.BooleanField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Announcement:
        return services.publish(self.actor, **data)


@document_resource
class AnnouncementDetail(_Announcement, ResourceDetailView):
    update_serializer = s.AnnouncementUpdateIn
    allow_delete = True

    def perform_update(self, obj: Announcement, data: dict[str, Any]) -> Announcement:
        return services.update(self.actor, obj, **data)

    def perform_delete(self, obj: Announcement) -> None:
        services.archive(self.actor, obj)


class PendingAnnouncementsView(TenantAPIView):
    required_permissions = {"GET": "announcement.read"}

    @extend_schema(
        tags=[TAG],
        summary="Announcements waiting for your acknowledgement",
        parameters=[TENANT_HEADER],
        responses={200: s.AnnouncementOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response(s.AnnouncementOut(services.pending_for(self.actor), many=True).data)


def _announcement(view: TenantAPIView, pk: Any) -> Announcement:
    base = Announcement.objects.select_related("section", "author__user")
    return policies.announcements.get(view.actor, "announcement.read", pk, base=base)


class AcknowledgeView(TenantAPIView):
    required_permissions = {"POST": "announcement.read"}

    @extend_schema(
        tags=[TAG],
        summary="Acknowledge an announcement addressed to you (idempotent)",
        parameters=[TENANT_HEADER],
        request=None,
        responses={204: None, **errors(401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        services.acknowledge(self.actor, _announcement(self, pk))
        return Response(status=204)


class RespondView(TenantAPIView):
    required_permissions = {"POST": "announcement.read"}

    @extend_schema(
        tags=[TAG],
        summary="Respond to an announcement (staff recipients)",
        parameters=[TENANT_HEADER],
        request=s.RespondIn,
        responses={201: s.ResponseOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        announcement = _announcement(self, pk)
        body = s.RespondIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.ResponseOut(services.respond(self.actor, announcement, **body.validated_data)).data, status=201
        )


class AckStatusView(TenantAPIView):
    required_permissions = {"GET": "announcement.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Who has acknowledged an announcement, and who has not",
        description="The author or the office.",
        parameters=[TENANT_HEADER],
        responses={200: s.AckStatusOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        announcement = policies.announcements.get(self.actor, "announcement.manage", pk)
        services.check_author(self.actor, announcement)
        return Response(s.AckStatusOut(services.acknowledgement_status(announcement)).data)


# ------------------------------------------------------------------------------------------------ threads
class _Thread(ResourceView):
    tag = TAG
    resource = policies.threads
    read_permission, write_permission = "message.read", "message.send"
    output_serializer = s.ThreadOut
    narrow_writes = True  # participants open their own threads (checked in the service)

    def base_queryset(self) -> QuerySet[Thread]:
        return Thread.objects.select_related("student", "staff__membership__user", "guardian", "subject")


def _awaiting(qs: QuerySet[Any], value: bool) -> QuerySet[Any]:
    return qs.filter(awaiting_reply_since__isnull=not value)


@document_resource
class ThreadList(_Thread, ResourceListView):
    create_serializer = s.ThreadIn
    filters = [
        Filter("student_id", "student_id", serializers.UUIDField()),
        Filter(
            "awaiting_reply",
            "",
            serializers.BooleanField(),
            "Waiting for the teacher's reply.",
            apply=_awaiting,
        ),
    ]

    def perform_create(self, data: dict[str, Any]) -> Thread:
        return services.open_thread(self.actor, **data)


@document_resource
class ThreadDetail(_Thread, ResourceDetailView):
    pass


class ThreadMessagesView(TenantAPIView):
    required_permissions = {"GET": "message.read", "POST": "message.send"}

    @extend_schema(
        tags=[TAG],
        summary="Messages of a thread",
        parameters=[TENANT_HEADER],
        responses={200: s.MessageOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        thread = policies.threads.get(self.actor, "message.read", pk)
        return Response(s.MessageOut(thread.messages.select_related("author__user"), many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Send a message in a thread you take part in",
        parameters=[TENANT_HEADER],
        request=s.MessageIn,
        responses={201: s.MessageOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        thread = policies.threads.get(self.actor, "message.read", pk)
        if not Thread.objects.filter(pk=thread.pk).filter(policies.participant(self.actor)).exists():
            self.permission_denied(request)  # the office may read every thread but only participants write
        body = s.MessageIn(data=request.data)
        body.is_valid(raise_exception=True)
        message = services.post_message(self.actor, thread, **body.validated_data)
        return Response(s.MessageOut(message).data, status=201)


# ------------------------------------------------------------------------------------------------ complaints
class _Complaint(ResourceView):
    tag = TAG
    resource = policies.complaints
    read_permission, write_permission = "complaint.read", "complaint.manage"
    output_serializer = s.ComplaintOut

    def base_queryset(self) -> QuerySet[Complaint]:
        return Complaint.objects.select_related("student", "raised_by__user", "assigned_to__user")


@document_resource
class ComplaintList(_Complaint, ResourceListView):
    create_serializer = s.ComplaintIn
    filters = [
        Filter("status", "status", serializers.ChoiceField(ComplaintStatus.choices)),
        Filter("category", "category", serializers.ChoiceField(ComplaintCategory.choices)),
        Filter("sentiment", "sentiment", serializers.ChoiceField(Sentiment.choices)),
    ]

    @classmethod
    def permission_map(cls) -> dict[str, str]:
        return {"GET": "complaint.read", "POST": "complaint.create"}

    def post(self, request: Request) -> Response:
        body = s.ComplaintIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(self.out(services.raise_complaint(self.actor, **body.validated_data)), status=201)


@document_resource
class ComplaintDetail(_Complaint, ResourceDetailView):
    update_serializer = s.ComplaintUpdateIn

    def perform_update(self, obj: Complaint, data: dict[str, Any]) -> Complaint:
        return services.handle_complaint(self.actor, obj, **data)


class SentimentView(TenantAPIView):
    required_permissions = {"GET": "complaint.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Parent sentiment: complaints and feedback by sentiment and category",
        description="Sentiment is what reporters chose; EduFlow does not infer it from text.",
        parameters=[TENANT_HEADER, OpenApiParameter("days", OpenApiTypes.INT, description="Default 90.")],
        responses={200: s.SentimentOut, **errors(400, 401, 403)},
    )
    def get(self, request: Request) -> Response:
        if DataScope.SCHOOL not in self.actor.scopes("complaint.manage"):
            self.permission_denied(request)
        days = serializers.IntegerField(min_value=1, max_value=730).run_validation(
            request.query_params.get("days") or 90
        )
        since = timezone.now() - datetime.timedelta(days=days)
        return Response(s.SentimentOut(services.sentiment_summary(self.actor.school, since)).data)
