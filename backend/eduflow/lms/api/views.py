"""LMS endpoints. ``lms.read`` (teachers: sections they teach; families: published content of their
sections), ``lms.manage`` (teachers for a section and subject they teach; the office school-wide) and
``lms.learn`` (students: progress, quiz attempts, joining live classes)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.academics.policies import sections
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
from eduflow.people.scoping import may_write

from .. import ai, policies, services
from ..models import Attempt, LearningPath, Lesson, LessonKind, LiveClass, PublishStatus, Quiz
from . import serializers as s

TAG = "lms"
READ, MANAGE, LEARN = "lms.read", "lms.manage", "lms.learn"


class _Content(ResourceView):
    tag = TAG
    read_permission, write_permission = READ, MANAGE
    narrow_writes = True  # a teacher for a section and subject they teach (checked in the services)


# ------------------------------------------------------------------------------------------------ lessons
class _Lesson(_Content):
    resource = policies.lessons
    output_serializer = s.LmsLessonOut

    def base_queryset(self) -> QuerySet[Lesson]:
        return Lesson.objects.select_related("section", "subject", "file")


@document_resource
class LessonList(_Lesson, ResourceListView):
    create_serializer = s.LmsLessonIn
    parser_classes = [JSONParser, MultiPartParser]
    filters = [
        Filter("section_id", "section_id", serializers.UUIDField()),
        Filter("subject_id", "subject_id", serializers.UUIDField()),
        Filter("kind", "kind", serializers.ChoiceField(LessonKind.choices)),
        Filter("status", "status", serializers.ChoiceField(PublishStatus.choices)),
        Filter("chapter", "chapter", serializers.CharField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Lesson:
        return services.create_lesson(self.actor, **data)


@document_resource
class LessonDetail(_Lesson, ResourceDetailView):
    update_serializer = s.LmsLessonUpdateIn

    def perform_update(self, obj: Lesson, data: dict[str, Any]) -> Lesson:
        return services.update_lesson(self.actor, obj, **data)


class LessonFileView(TenantAPIView):
    required_permissions = {"GET": READ}

    @extend_schema(
        tags=[TAG],
        summary="Download a lesson's attached file",
        parameters=[TENANT_HEADER],
        responses={
            (200, "application/octet-stream"): OpenApiResponse(OpenApiTypes.BINARY),
            **errors(401, 403, 404),
        },
    )
    def get(self, request: Request, pk: Any) -> HttpResponse:
        lesson = policies.lessons.get(self.actor, READ, pk, base=Lesson.objects.select_related("file"))
        if lesson.file is None:
            raise NotFound()
        return files.download(lesson.file)


class LessonProgressView(TenantAPIView):
    required_permissions = {"GET": READ, "POST": LEARN}

    @extend_schema(
        tags=[TAG],
        summary="Progress on a lesson, for the students the caller may see",
        parameters=[TENANT_HEADER],
        responses={200: s.ProgressOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        lesson = policies.lessons.get(self.actor, READ, pk)
        rows = policies.progress.queryset(self.actor, READ).filter(lesson=lesson).select_related("student")
        return Response(s.ProgressOut(rows, many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Report your progress on a lesson (it never goes back)",
        parameters=[TENANT_HEADER],
        request=s.ProgressIn,
        responses={200: s.ProgressOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        lesson = policies.lessons.get(self.actor, READ, pk)
        body = s.ProgressIn(data=request.data)
        body.is_valid(raise_exception=True)
        row = services.record_progress(self.actor, lesson, **body.validated_data)
        row.refresh_from_db()
        return Response(s.ProgressOut(row).data)


# ------------------------------------------------------------------------------------------------ quizzes
def _reveal(view: TenantAPIView, quiz: Quiz) -> bool:
    if may_write(view.actor, MANAGE, quiz.section, quiz.subject):
        return True
    return Attempt.objects.filter(quiz=quiz, student__membership=view.actor.membership).exists()


class _Quiz(_Content):
    resource = policies.quizzes
    output_serializer = s.QuizOut

    def base_queryset(self) -> QuerySet[Quiz]:
        return Quiz.objects.select_related("section", "subject").prefetch_related("questions")

    def out(self, obj: Any) -> dict[str, Any]:
        fresh = self.base_queryset().get(pk=obj.pk)
        return dict(s.QuizOut(fresh, context={"reveal": _reveal(self, fresh)}).data)


@document_resource
class QuizList(_Quiz, ResourceListView):
    create_serializer = s.QuizIn
    filters = [
        Filter("section_id", "section_id", serializers.UUIDField()),
        Filter("subject_id", "subject_id", serializers.UUIDField()),
        Filter("status", "status", serializers.ChoiceField(PublishStatus.choices)),
    ]

    def get(self, request: Request) -> Response:
        response = super().get(request)
        for row in response.data["results"]:
            for q in row["questions"]:
                q["answer"] = q["explanation"] = None  # lists never carry answer keys
        return response

    def perform_create(self, data: dict[str, Any]) -> Quiz:
        return services.create_quiz(self.actor, **data)


@document_resource
class QuizDetail(_Quiz, ResourceDetailView):
    update_serializer = s.QuizUpdateIn

    def get(self, request: Request, pk: Any) -> Response:
        return Response(self.out(self.get_object(pk, READ)))

    def perform_update(self, obj: Quiz, data: dict[str, Any]) -> Quiz:
        return services.update_quiz(self.actor, obj, **data)


class QuizAttemptsView(TenantAPIView):
    required_permissions = {"GET": READ, "POST": LEARN}

    @extend_schema(
        tags=[TAG],
        summary="Attempts at a quiz, for the students the caller may see",
        parameters=[TENANT_HEADER],
        responses={200: s.AttemptOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        quiz = policies.quizzes.get(self.actor, READ, pk)
        rows = policies.attempts.queryset(self.actor, READ).filter(quiz=quiz).select_related("student")
        return Response(s.AttemptOut(rows, many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Submit an attempt (the signed-in student)",
        description="One answer index per question, in order; -1 skips. Scored at once; the best counts.",
        parameters=[TENANT_HEADER],
        request=s.AttemptIn,
        responses={201: s.AttemptOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        quiz = policies.quizzes.get(self.actor, READ, pk)
        body = s.AttemptIn(data=request.data)
        body.is_valid(raise_exception=True)
        result = services.attempt(self.actor, quiz, **body.validated_data)
        return Response(
            s.AttemptOut(Attempt.objects.select_related("student").get(pk=result.pk)).data, status=201
        )


class QuizGenerateView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Draft questions with the configured AI provider",
        description=(
            "Returns draft questions for the teacher to review; nothing is saved or published. Without a "
            "configured provider: `503 ai_unavailable`. EduFlow never invents questions itself."
        ),
        parameters=[TENANT_HEADER],
        request=s.QuizGenerateIn,
        responses={200: s.QuizDraftOut, **errors(400, 401, 403, 503)},
    )
    def post(self, request: Request) -> Response:
        body = s.QuizGenerateIn(data=request.data)
        body.is_valid(raise_exception=True)
        questions = ai.generate(**body.validated_data)
        return Response({"questions": questions, "note": "AI draft: review every question before use."})


# ------------------------------------------------------------------------------------------------ paths
class _Path(_Content):
    resource = policies.paths
    output_serializer = s.PathOut

    def base_queryset(self) -> QuerySet[LearningPath]:
        return LearningPath.objects.select_related("section", "subject").prefetch_related("steps")


@document_resource
class PathList(_Path, ResourceListView):
    create_serializer = s.PathIn
    filters = [Filter("section_id", "section_id", serializers.UUIDField())]

    def perform_create(self, data: dict[str, Any]) -> LearningPath:
        return services.create_path(self.actor, **data)


@document_resource
class PathDetail(_Path, ResourceDetailView):
    update_serializer = s.PathUpdateIn

    def perform_update(self, obj: LearningPath, data: dict[str, Any]) -> LearningPath:
        return services.update_path(self.actor, obj, **data)


class PathProgressView(TenantAPIView):
    required_permissions = {"GET": READ}

    @extend_schema(
        tags=[TAG],
        summary="Progress through a learning path, per student the caller may see",
        parameters=[TENANT_HEADER],
        responses={200: s.PathProgressOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        path = policies.paths.get(self.actor, READ, pk)
        students = people_policies.students.queryset(self.actor, READ).filter(
            enrollments__section=path.section, enrollments__status="active"
        )
        rows = [{"student": st, **services.path_progress(path, st)} for st in students.order_by("first_name")]
        return Response(s.PathProgressOut(rows, many=True).data)


# ------------------------------------------------------------------------------------------------ live
class _Live(_Content):
    resource = policies.live_classes
    output_serializer = s.LiveOut

    def base_queryset(self) -> QuerySet[LiveClass]:
        return LiveClass.objects.select_related("section", "subject")


@document_resource
class LiveList(_Live, ResourceListView):
    create_serializer = s.LiveIn
    filters = [
        Filter("section_id", "section_id", serializers.UUIDField()),
        Filter("from", "starts_at__gte", serializers.DateTimeField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> LiveClass:
        return services.schedule_live(self.actor, **data)


@document_resource
class LiveDetail(_Live, ResourceDetailView):
    update_serializer = s.LiveUpdateIn

    def perform_update(self, obj: LiveClass, data: dict[str, Any]) -> LiveClass:
        return services.update_live(self.actor, obj, **data)


class LiveJoinView(TenantAPIView):
    required_permissions = {"POST": LEARN}

    @extend_schema(
        tags=[TAG],
        summary="Join a live class (records attendance, returns the provider's link)",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.JoinOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        live = policies.live_classes.get(self.actor, READ, pk)
        return Response({"join_url": services.join_live(self.actor, live)})


# ------------------------------------------------------------------------------------------------ analytics
class AnalyticsView(TenantAPIView):
    required_permissions = {"GET": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Learning analytics for a section",
        description="Completion per published lesson, quiz results (best attempts) and per-student totals.",
        parameters=[
            TENANT_HEADER,
            OpenApiParameter("section_id", OpenApiTypes.UUID, required=True),
            OpenApiParameter("subject_id", OpenApiTypes.UUID),
        ],
        responses={200: s.AnalyticsOut, **errors(400, 401, 403, 404)},
    )
    def get(self, request: Request) -> Response:
        field = serializers.UUIDField()
        section_id = field.run_validation(request.query_params.get("section_id") or None)
        subject_id = request.query_params.get("subject_id")
        section = sections.get(self.actor, MANAGE, section_id)
        data = services.analytics(section, field.run_validation(subject_id) if subject_id else None)
        return Response(s.AnalyticsOut(data).data)
