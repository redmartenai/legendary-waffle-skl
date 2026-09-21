from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.academics.access import children_of, teacher_class_ids
from apps.academics.models import ClassGroup, Student
from apps.accounts.models import FAMILY_ROLES, MANAGEMENT_ROLES, STAFF_ROLES, Membership, Role
from apps.core.api import SchoolAPIView
from apps.notifications.models import Category, Priority
from apps.notifications.services import notify

from .models import Announcement, AnnouncementAck


def _visible_to(request):
    audiences = [Announcement.Audience.EVERYONE]
    class_ids: set = set()
    if request.roles & FAMILY_ROLES:
        audiences.append(Announcement.Audience.FAMILIES)
        class_ids |= {kid.class_group_id for kid in children_of(request.user)}
        class_ids |= set(Student.objects.filter(user=request.user).values_list("class_group_id", flat=True))
    if request.roles & STAFF_ROLES:
        audiences.append(Announcement.Audience.STAFF)
        class_ids |= teacher_class_ids(request.user)
    if request.roles & MANAGEMENT_ROLES:
        return Announcement.objects.all()
    return Announcement.objects.filter(
        Q(audience__in=audiences) | Q(audience=Announcement.Audience.CLASSES, class_groups__in=class_ids)
    ).distinct()


def announcement_payload(item: Announcement, acked: bool) -> dict:
    return {
        "id": str(item.id),
        "title": item.title,
        "body": item.body,
        "kind": item.kind,
        "audience": item.audience,
        "requires_ack": item.requires_ack,
        "acknowledged": acked,
        "published_at": item.published_at.isoformat(),
        "author": item.created_by.full_name if item.created_by else None,
    }


class CreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=120)
    body = serializers.CharField(max_length=4000)
    kind = serializers.ChoiceField(choices=Announcement.Kind.choices, default=Announcement.Kind.GENERAL)
    audience = serializers.ChoiceField(choices=Announcement.Audience.choices, default=Announcement.Audience.EVERYONE)
    class_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    requires_ack = serializers.BooleanField(default=False)


class AnnouncementListView(SchoolAPIView):
    def get(self, request):
        items = list(_visible_to(request).select_related("created_by")[:40])
        acked = set(
            AnnouncementAck.objects.filter(user=request.user, announcement__in=items).values_list("announcement_id", flat=True)
        )
        return Response({"items": [announcement_payload(i, i.id in acked) for i in items]})

    def post(self, request):
        if not (request.roles & (MANAGEMENT_ROLES | {Role.TEACHER})):
            raise PermissionDenied()
        data = CreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        audience = data.validated_data["audience"]
        groups = list(ClassGroup.objects.filter(id__in=data.validated_data.get("class_ids", [])))
        if not (request.roles & MANAGEMENT_ROLES):
            # Teachers may only post to their own classes.
            mine = teacher_class_ids(request.user)
            if audience != Announcement.Audience.CLASSES or not groups or any(g.id not in mine for g in groups):
                raise PermissionDenied("Teachers can post to their own classes only.")
        if audience == Announcement.Audience.CLASSES and not groups:
            raise ValidationError({"class_ids": "Choose at least one class."})

        item = Announcement.objects.create(
            title=data.validated_data["title"],
            body=data.validated_data["body"],
            kind=data.validated_data["kind"],
            audience=audience,
            requires_ack=data.validated_data["requires_ack"],
            created_by=request.user,
            published_at=timezone.now(),
        )
        if groups:
            item.class_groups.set(groups)
        _notify_audience(request, item, groups)
        return Response(announcement_payload(item, False), status=status.HTTP_201_CREATED)


def _notify_audience(request, item: Announcement, groups) -> None:
    from apps.academics.models import StudentGuardian

    recipients: set = set()
    if item.audience in {Announcement.Audience.EVERYONE, Announcement.Audience.STAFF}:
        recipients |= {m.user for m in Membership.objects.filter(role__in=STAFF_ROLES, is_active=True).select_related("user")}
    if item.audience in {Announcement.Audience.EVERYONE, Announcement.Audience.FAMILIES}:
        recipients |= {m.user for m in Membership.objects.filter(role__in=FAMILY_ROLES, is_active=True).select_related("user")}
    if item.audience == Announcement.Audience.CLASSES:
        students = Student.objects.filter(class_group__in=groups, is_active=True)
        recipients |= {link.user for link in StudentGuardian.objects.filter(student__in=students).select_related("user")}
        recipients |= {s.user for s in students.select_related("user") if s.user}
    recipients.discard(request.user)
    notify(
        recipients,
        school=request.school,
        category=Category.SAFETY if item.kind == Announcement.Kind.SAFETY else Category.ANNOUNCEMENT,
        title=item.title,
        body=item.body[:180],
        data={"announcement_id": str(item.id), "type": "announcement"},
        dedupe_key=f"announcement:{item.id}",
        priority=Priority.CRITICAL if item.kind == Announcement.Kind.SAFETY else Priority.NORMAL,
    )


class AnnouncementAckView(SchoolAPIView):
    def post(self, request, announcement_id):
        item = _visible_to(request).filter(id=announcement_id).first()
        if item is None:
            raise Http404
        AnnouncementAck.objects.get_or_create(announcement=item, user=request.user, defaults={"acked_at": timezone.now()})
        return Response({"acknowledged": True})
