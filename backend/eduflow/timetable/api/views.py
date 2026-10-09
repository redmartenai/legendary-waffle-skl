"""Timetable endpoints. Thin: authorization in the base views and scope rules, rules in the services.

* Timetables, periods and slots are managed school-wide (``timetable.manage``, like every Phase 3 write).
* Lessons are the first resource a narrower scope may write: a teacher records their own (``lesson.manage``
  with ``self``). The view loads the slot or lesson through the caller's ``lesson.manage`` scope (404 outside
  it), and the service re-checks that the caller teaches it.
* Schedules are authorised by their subject: the teacher, student or section must be visible to the caller
  under its own read permission (``staff.read``, ``student.read``, ``section.read``) **and** under
  ``timetable.read``, each through that resource's scope rules. The schedule then shows the subject's whole
  plan, including a student's earlier section within the requested dates (after a transfer).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

from django.db.models import Model, QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
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
from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource
from eduflow.core.api import TENANT_HEADER, errors
from eduflow.people import policies as people_policies
from eduflow.people.models import StaffProfile, Student

from .. import policies, selectors, services
from ..models import Lesson, LessonStatus, Period, SlotKind, Timetable, TimetableSlot
from . import serializers as s

TAG = "timetable"
UUID = serializers.UUIDField
WEEKDAY_FILTER = serializers.ChoiceField([(d, name) for d, name in services.WEEKDAYS.items()])


# ------------------------------------------------------------------------------------------------ timetables
class _Timetable(ResourceView):
    tag = TAG
    resource = policies.timetables
    read_permission, write_permission = "timetable.read", "timetable.manage"
    output_serializer = s.TimetableOut

    def base_queryset(self) -> QuerySet[Timetable]:
        return Timetable.objects.select_related("academic_year", "term")


@document_resource
class TimetableList(_Timetable, ResourceListView):
    create_serializer = s.TimetableCreateIn
    filters = [
        Filter("academic_year_id", "academic_year_id", UUID()),
        Filter("term_id", "term_id", UUID()),
        Filter("status", "status", s.TIMETABLE_STATUS),
    ]

    def perform_create(self, data: dict[str, Any]) -> Timetable:
        return services.create_timetable(self.actor, **data)


@document_resource
class TimetableDetail(_Timetable, ResourceDetailView):
    update_serializer = s.TimetableUpdateIn
    allow_delete = True

    def perform_update(self, obj: Timetable, data: dict[str, Any]) -> Timetable:
        return services.update_timetable(self.actor, obj, **data)

    def perform_delete(self, obj: Timetable) -> None:
        services.delete_timetable(self.actor, obj)


class _TimetableAction(TenantAPIView):
    required_permissions = {"POST": "timetable.manage"}

    def load(self, pk: Any) -> Timetable:
        if DataScope.SCHOOL not in self.actor.scopes("timetable.manage"):
            self.permission_denied(self.request)
        return policies.timetables.get(self.actor, "timetable.manage", pk)

    def respond(self, timetable: Timetable, status: int = 200) -> Response:
        fresh = Timetable.objects.select_related("academic_year", "term").get(pk=timetable.pk)
        return Response(s.TimetableOut(fresh).data, status=status)


class TimetablePublishView(_TimetableAction):
    @extend_schema(
        tags=[TAG],
        summary="Publish a draft timetable",
        description=(
            "Requires `timetable.manage`. The timetable's slots then appear in schedules and take part in "
            "clash checks against every other published timetable (`409` naming the clashes)."
        ),
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.TimetableOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.respond(services.publish_timetable(self.actor, self.load(pk)))


class TimetableArchiveView(_TimetableAction):
    @extend_schema(
        tags=[TAG],
        summary="Archive a published timetable",
        description="Requires `timetable.manage`. It leaves schedules; its lessons stay as history.",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.TimetableOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.respond(services.archive_timetable(self.actor, self.load(pk)))


class TimetableCopyView(_TimetableAction):
    @extend_schema(
        tags=[TAG],
        summary="Copy a timetable into a new draft",
        description=(
            "Requires `timetable.manage`. Same academic year; periods and slots are copied, except lesson "
            "slots whose teacher assignment has ended."
        ),
        parameters=[TENANT_HEADER],
        request=s.TimetableCopyIn,
        responses={201: s.TimetableOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        source = self.load(pk)
        body = s.TimetableCopyIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.copy_timetable(self.actor, source, **body.validated_data), status=201)


# ------------------------------------------------------------------------------------------------ periods
class _Period(ResourceView):
    tag = TAG
    resource = policies.periods
    read_permission, write_permission = "timetable.read", "timetable.manage"
    output_serializer = s.PeriodOut


@document_resource
class PeriodList(_Period, ResourceListView):
    create_serializer = s.PeriodCreateIn
    filters = [Filter("timetable_id", "timetable_id", UUID())]

    def perform_create(self, data: dict[str, Any]) -> Period:
        return services.create_period(self.actor, **data)


@document_resource
class PeriodDetail(_Period, ResourceDetailView):
    update_serializer = s.PeriodUpdateIn
    allow_delete = True

    def perform_update(self, obj: Period, data: dict[str, Any]) -> Period:
        return services.update_period(self.actor, obj, **data)

    def perform_delete(self, obj: Period) -> None:
        services.delete_period(self.actor, obj)


# ------------------------------------------------------------------------------------------------ slots
SLOT_RELATED = ("period", "section", "subject", "room", "staff__membership__user")


class _Slot(ResourceView):
    tag = TAG
    resource = policies.slots
    read_permission, write_permission = "timetable.read", "timetable.manage"
    output_serializer = s.SlotOut

    def base_queryset(self) -> QuerySet[TimetableSlot]:
        return TimetableSlot.objects.select_related(*SLOT_RELATED)


@document_resource
class SlotList(_Slot, ResourceListView):
    create_serializer = s.SlotCreateIn
    filters = [
        Filter("timetable_id", "timetable_id", UUID()),
        Filter("section_id", "section_id", UUID()),
        Filter("staff_id", "staff_id", UUID()),
        Filter("room_id", "room_id", UUID()),
        Filter("weekday", "weekday", WEEKDAY_FILTER),
        Filter("kind", "kind", serializers.ChoiceField(SlotKind.choices)),
    ]

    def perform_create(self, data: dict[str, Any]) -> TimetableSlot:
        return services.create_slot(self.actor, **data)


@document_resource
class SlotDetail(_Slot, ResourceDetailView):
    update_serializer = s.SlotUpdateIn
    allow_delete = True

    def perform_update(self, obj: TimetableSlot, data: dict[str, Any]) -> TimetableSlot:
        return services.update_slot(self.actor, obj, **data)

    def perform_delete(self, obj: TimetableSlot) -> None:
        services.delete_slot(self.actor, obj)


# ------------------------------------------------------------------------------------------------ lessons
class _Lesson(ResourceView):
    tag = TAG
    resource = policies.lessons
    read_permission, write_permission = "lesson.read", "lesson.manage"
    output_serializer = s.LessonOut

    def base_queryset(self) -> QuerySet[Lesson]:
        return Lesson.objects.select_related("section", "subject", "staff__membership__user")


@document_resource
class LessonList(_Lesson, ResourceListView):
    create_serializer = s.LessonCreateIn
    filters = [
        Filter("section_id", "section_id", UUID()),
        Filter("staff_id", "staff_id", UUID()),
        Filter("slot_id", "slot_id", UUID()),
        Filter("status", "status", serializers.ChoiceField(LessonStatus.choices)),
        Filter("date_from", "date__gte", serializers.DateField()),
        Filter("date_to", "date__lte", serializers.DateField()),
    ]

    def post(self, request: Request) -> Response:
        # Not the school-wide write rule of ResourceListView: a teacher records their own lessons.
        body = s.LessonCreateIn(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        slot = policies.slots.get(self.actor, "lesson.manage", data.pop("slot_id"))
        return Response(self.out(services.record_lesson(self.actor, slot, **data)), status=201)


@document_resource
class LessonDetail(_Lesson, ResourceDetailView):
    update_serializer = s.LessonUpdateIn

    def patch(self, request: Request, pk: Any) -> Response:
        # A teacher updates their own lessons (the service re-checks); others need a school-wide grant.
        lesson = self.get_object(pk, "lesson.manage")
        body = s.LessonUpdateIn(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        return Response(self.out(services.update_lesson(self.actor, lesson, **body.validated_data)))


# ------------------------------------------------------------------------------------------------ schedules
_Handler = Callable[..., Response]

SCHEDULE_PARAMETERS = [
    TENANT_HEADER,
    OpenApiParameter("date_from", datetime.date, description="Defaults to this week's Monday."),
    OpenApiParameter(
        "date_to", datetime.date, description="Defaults to date_from + 6 days; at most 42 days."
    ),
]


class _Schedule(TenantAPIView):
    required_permissions = {"GET": "timetable.read"}

    def window(self, request: Request) -> tuple[datetime.date, datetime.date]:
        query = s.ScheduleQuery(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        today = services.school_today(self.actor)
        start = query.validated_data.get("date_from") or today - datetime.timedelta(days=today.weekday())
        end = query.validated_data.get("date_to") or start + datetime.timedelta(days=6)
        if end < start:
            raise ValidationError({"date_to": ["date_to must not be before date_from."]})
        if (end - start).days + 1 > selectors.MAX_DAYS:
            raise ValidationError({"date_to": [f"A schedule covers at most {selectors.MAX_DAYS} days."]})
        return start, end

    def subject[M: Model](self, resource: ScopedResource[M], read_permission: str, pk: Any) -> M:
        """The teacher, student or section asked about, visible under both permissions; otherwise 404."""
        found = resource.get(self.actor, read_permission, pk)
        if not resource.queryset(self.actor, "timetable.read").filter(pk=found.pk).exists():
            raise NotFound()
        return found

    def school_slots(self) -> QuerySet[TimetableSlot]:
        return TimetableSlot.objects.for_school(self.actor.school)

    def respond(self, start: datetime.date, end: datetime.date, entries: list[selectors.Entry]) -> Response:
        payload = {"date_from": start, "date_to": end, "entries": selectors.sort(entries)}
        return Response(s.ScheduleOut(payload).data)


def _schedule_docs(summary: str, description: str) -> Callable[[_Handler], _Handler]:
    return extend_schema(
        tags=[TAG],
        summary=summary,
        description=description,
        parameters=SCHEDULE_PARAMETERS,
        responses={200: s.ScheduleOut, **errors(400, 401, 403, 404)},
    )


class MyScheduleView(_Schedule):
    @_schedule_docs(
        "My schedule",
        "Requires `timetable.read`. What the caller teaches (as a teacher) and attends (as a student).",
    )
    def get(self, request: Request) -> Response:
        start, end = self.window(request)
        slots = self.school_slots()
        entries: list[selectors.Entry] = []
        staff = StaffProfile.objects.filter(membership=self.actor.membership).first()
        if staff is not None:
            entries += selectors.staff_schedule(slots, staff, start, end, role="teacher")
        student = Student.objects.filter(membership=self.actor.membership).first()
        if student is not None:
            entries += selectors.student_schedule(slots, student, start, end, role="student")
        return self.respond(start, end, entries)


class StaffScheduleView(_Schedule):
    @_schedule_docs(
        "A teacher's schedule",
        "Requires `timetable.read` and `staff.read`, both covering the staff record (a teacher: their own).",
    )
    def get(self, request: Request, pk: Any) -> Response:
        staff = self.subject(people_policies.staff, "staff.read", pk)
        start, end = self.window(request)
        return self.respond(start, end, selectors.staff_schedule(self.school_slots(), staff, start, end))


class StudentScheduleView(_Schedule):
    @_schedule_docs(
        "A student's schedule",
        "Requires `timetable.read` and `student.read`, both covering the student: a parent sees their "
        "children's, a student their own, a teacher those of the sections they teach.",
    )
    def get(self, request: Request, pk: Any) -> Response:
        student = self.subject(people_policies.students, "student.read", pk)
        start, end = self.window(request)
        return self.respond(start, end, selectors.student_schedule(self.school_slots(), student, start, end))


class SectionScheduleView(_Schedule):
    @_schedule_docs(
        "A section's schedule",
        "Requires `timetable.read` and `section.read`, both covering the section.",
    )
    def get(self, request: Request, pk: Any) -> Response:
        section = self.subject(sections, "section.read", pk)
        start, end = self.window(request)
        return self.respond(start, end, selectors.section_schedule(self.school_slots(), section, start, end))
