from django.db.models import Q
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.academics.access import children_of, teacher_class_ids
from apps.academics.models import Student
from apps.accounts.models import FAMILY_ROLES, MANAGEMENT_ROLES, STAFF_ROLES, Role
from apps.core.api import SchoolAPIView

from .delivery import CHANNELS, deliver, estimate, groups_for
from .models import Announcement, AnnouncementAck, AnnouncementRead


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
    if Role.PARENT in request.roles:
        audiences.append(Announcement.Audience.PARENTS)
    if request.roles & MANAGEMENT_ROLES:
        return Announcement.objects.exclude(status=Announcement.Status.DRAFT)
    routes: set = set()
    if request.roles & FAMILY_ROLES:
        from apps.transport.models import StudentTransport

        kids = [k.id for k in children_of(request.user)] + list(Student.objects.filter(user=request.user).values_list("id", flat=True))
        routes = set(StudentTransport.objects.filter(student_id__in=kids, is_active=True).values_list("route_id", flat=True))
    items = (
        Announcement.objects.filter(published_at__lte=timezone.now())
        .exclude(status=Announcement.Status.DRAFT)
        .filter(
            Q(audience__in=audiences)
            | Q(audience=Announcement.Audience.CLASSES, class_groups__in=class_ids)
            | Q(audience=Announcement.Audience.ROUTE, route_id__in=routes)
        )
        .distinct()
    )
    if not (request.roles - {Role.STUDENT}):
        # "Parents only" notices skip the students' own logins.
        items = items.exclude(parents_only=True)
    return items


def audience_label(item: Announcement) -> str:
    if item.audience == Announcement.Audience.ROUTE:
        return f"Families on {item.route.name}" if item.route else "Families on a bus route"
    if item.audience != Announcement.Audience.CLASSES:
        return Announcement.Audience(item.audience).label
    labels = sorted({g.short_label for g in item.class_groups.all()})
    grades = sorted({g.grade for g in item.class_groups.all()}, key=lambda g: (not g.isdigit(), int(g) if g.isdigit() else 0, g))
    if len(labels) > 3 and grades:
        return f"Grade {grades[0]}" if len(grades) == 1 else f"Grades {grades[0]}–{grades[-1]}"
    return ", ".join(labels)


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
        "scheduled": item.delivered_at is None and item.published_at > timezone.now(),
        "channels": item.channels or ["push", "in_app"],
        "attachment": {"name": item.attachment_name, "size": item.attachment_size, "url": f"/announcements/{item.id}/file"} if item.attachment else None,
        "audience_label": audience_label(item),
        "author": item.created_by.full_name if item.created_by else None,
        "event": {
            "starts_at": item.event_starts_at.isoformat(),
            "ends_at": item.event_ends_at.isoformat() if item.event_ends_at else None,
            "location": item.location,
        }
        if item.event_starts_at
        else None,
    }


class CreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=80)
    body = serializers.CharField(max_length=2000)
    kind = serializers.ChoiceField(choices=Announcement.Kind.choices, default=Announcement.Kind.GENERAL)
    audience = serializers.ChoiceField(choices=Announcement.Audience.choices, default=Announcement.Audience.EVERYONE)
    class_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    grades = serializers.ListField(child=serializers.CharField(max_length=20), required=False)
    route_id = serializers.UUIDField(required=False)
    channels = serializers.ListField(child=serializers.ChoiceField(choices=CHANNELS), required=False)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    requires_ack = serializers.BooleanField(default=False)


def _list(request, key):
    """Lists arrive as JSON arrays, or as repeated multipart fields."""
    if hasattr(request.data, "getlist"):
        values = request.data.getlist(key)
        if len(values) == 1 and isinstance(values[0], str) and values[0].startswith("["):
            import json

            return json.loads(values[0])
        return values
    return request.data.get(key) or []


class AnnouncementListView(SchoolAPIView):
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        items = list(_visible_to(request).select_related("created_by").prefetch_related("class_groups")[:40])
        acked = set(
            AnnouncementAck.objects.filter(user=request.user, announcement__in=items).values_list("announcement_id", flat=True)
        )
        return Response({"items": [announcement_payload(i, i.id in acked) for i in items]})

    def post(self, request):
        if not (request.roles & (MANAGEMENT_ROLES | {Role.TEACHER})):
            raise PermissionDenied()
        raw = {k: request.data.get(k) for k in ("title", "body", "kind", "audience", "scheduled_at", "requires_ack", "route_id") if request.data.get(k) not in (None, "")}
        raw.update({k: _list(request, k) for k in ("class_ids", "grades", "channels") if _list(request, k)})
        data = CreateSerializer(data=raw)
        data.is_valid(raise_exception=True)
        audience = data.validated_data["audience"]
        groups = groups_for(data.validated_data.get("grades", []), data.validated_data.get("class_ids", []))
        if groups and audience in (Announcement.Audience.FAMILIES, Announcement.Audience.EVERYONE):
            audience = Announcement.Audience.CLASSES
        if not (request.roles & MANAGEMENT_ROLES):
            # Teachers may only post to their own classes, in the app.
            mine = teacher_class_ids(request.user)
            if audience != Announcement.Audience.CLASSES or not groups or any(g.id not in mine for g in groups):
                raise PermissionDenied("Teachers can post to their own classes only.")
            if set(data.validated_data.get("channels", [])) - {"push", "in_app"}:
                raise PermissionDenied("Only the principal's office can send SMS, WhatsApp or email.")
        if audience == Announcement.Audience.CLASSES and not groups:
            raise ValidationError({"class_ids": "Choose at least one grade or section."})
        route = None
        if audience == Announcement.Audience.ROUTE:
            from apps.transport.models import Route

            route = Route.objects.filter(id=data.validated_data.get("route_id")).first()
            if route is None or not (request.roles & (MANAGEMENT_ROLES | {Role.TRANSPORT_MANAGER})):
                raise ValidationError({"route_id": "Choose a route."})
        channels = data.validated_data.get("channels") or ["push", "in_app"]
        when = data.validated_data.get("scheduled_at")
        now = timezone.now()
        if when and when < now - timezone.timedelta(minutes=1):
            raise ValidationError({"scheduled_at": "Pick a time in the future."})
        upload = request.FILES.get("attachment")
        if upload is not None and upload.size > 10 * 1024 * 1024:
            raise ValidationError({"attachment": "Attachments can be up to 10 MB."})

        item = Announcement.objects.create(
            title=data.validated_data["title"],
            body=data.validated_data["body"],
            kind=data.validated_data["kind"],
            audience=audience,
            requires_ack=data.validated_data["requires_ack"],
            created_by=request.user,
            published_at=when if when and when > now else now,
            channels=channels,
            route=route,
            attachment=upload or "",
            attachment_name=upload.name[:120] if upload else "",
            attachment_size=upload.size if upload else 0,
        )
        if groups:
            item.class_groups.set(groups)
        counts = {} if item.published_at > now else deliver(item, exclude=request.user)
        return Response({**announcement_payload(item, False), "delivered": counts}, status=status.HTTP_201_CREATED)


class EstimateView(SchoolAPIView):
    """How many families and staff an announcement would reach, per channel."""

    allowed_roles = MANAGEMENT_ROLES

    def post(self, request):
        audience = request.data.get("audience") or Announcement.Audience.EVERYONE
        if audience not in Announcement.Audience.values:
            raise ValidationError({"audience": "Unknown audience."})
        groups = groups_for(request.data.get("grades") or [], request.data.get("class_ids") or [])
        if groups:
            audience = Announcement.Audience.CLASSES
        route = None
        if audience == Announcement.Audience.ROUTE:
            from apps.transport.models import Route

            route = Route.objects.filter(id=request.data.get("route_id")).first()
        return Response(estimate(audience, groups, route))


class AnnouncementFileView(SchoolAPIView):
    def get(self, request, announcement_id):
        item = _visible_to(request).filter(id=announcement_id).first()
        if item is None or not item.attachment:
            raise Http404
        mark_read(item, request.user, "in_app")
        return FileResponse(item.attachment.open("rb"), as_attachment=True, filename=item.attachment_name or "attachment")


class AnnouncementAckView(SchoolAPIView):
    def post(self, request, announcement_id):
        item = _visible_to(request).filter(id=announcement_id).first()
        if item is None:
            raise Http404
        AnnouncementAck.objects.get_or_create(announcement=item, user=request.user, defaults={"acked_at": timezone.now()})
        mark_read(item, request.user, "in_app")
        return Response({"acknowledged": True})


def mark_read(item: Announcement, user, channel: str) -> None:
    AnnouncementRead.objects.get_or_create(announcement=item, user=user, channel=channel, defaults={"read_at": timezone.now()})


class AnnouncementReadView(SchoolAPIView):
    """The app (or the link in an SMS, WhatsApp or email) reports that someone opened an announcement."""

    def post(self, request, announcement_id):
        item = _visible_to(request).filter(id=announcement_id).first()
        if item is None:
            raise Http404
        channel = request.data.get("channel") or "in_app"
        if channel not in CHANNELS:
            raise ValidationError({"channel": "Unknown channel."})
        mark_read(item, request.user, channel)
        return Response({"read": True})
