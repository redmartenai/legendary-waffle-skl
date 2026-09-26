"""Console: communication page (PCommunication).

Announcements (compose, drafts, send now or schedule, read rates), circulars with acknowledgement rates,
the principal's messages and the meeting requests parents send. Delivery reuses ``apps.announcements.delivery``.
"""

import json
from datetime import datetime

from django.db.models import Count
from django.http import Http404
from django.urls import path
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.academics.models import ClassGroup, StudentGuardian
from apps.accounts.audit import audit
from apps.announcements import delivery
from apps.announcements.models import Announcement, AnnouncementAck, AnnouncementRead, ChannelDelivery, SmsCreditTopUp
from apps.core.api import SchoolAPIView
from apps.core.utils import school_now, school_today
from apps.messaging import services as chat
from apps.messaging.models import Conversation, ConversationMember, Meeting
from apps.notifications.models import Category, Notification
from apps.notifications.services import notify
from apps.principal.views import GRADE_ORDER, _grade_key, _pct

from .common import CONSOLE_ROLES

SCOPES = ("school", "grades", "sections", "staff")
BODY_LIMIT = 2000


# ------------------------------------------------------------------ audience


def _audience_for(scope: str, grades: list, class_ids: list, parents_only: bool) -> tuple[str, list]:
    """The composer's chips (scope + "Parents only") as an Announcement audience and class groups."""
    if scope not in SCOPES:
        raise ValidationError({"scope": "Choose who it goes to."})
    if scope == "staff":
        return Announcement.Audience.STAFF, []
    if scope == "school":
        return (Announcement.Audience.PARENTS if parents_only else Announcement.Audience.EVERYONE), []
    groups = delivery.groups_for(grades if scope == "grades" else [], class_ids if scope == "sections" else [])
    return Announcement.Audience.CLASSES, groups


def _range(grades: list[str]) -> str:
    """["6", "7", "8"] -> "6–8"; ["Nursery", …, "5"] -> "N–5"; a gap -> "6, 8"."""
    ordered = sorted(set(grades), key=_grade_key)
    idx = [_grade_key(g) for g in ordered]
    short = lambda g: {"Nursery": "N"}.get(g, g)  # noqa: E731
    if len(ordered) > 1 and idx == list(range(idx[0], idx[0] + len(idx))):
        return f"{short(ordered[0])}–{short(ordered[-1])}"
    return ", ".join(short(g) for g in ordered)


def _all_sections() -> dict[str, list]:
    by_grade: dict[str, list] = {}
    for g in ClassGroup.objects.all():
        by_grade.setdefault(g.grade, []).append(g)
    return by_grade


def audience_of(item: Announcement, by_grade: dict | None = None) -> dict:
    """What the composer and the lists need to show and restore an announcement's audience."""
    if item.audience == Announcement.Audience.STAFF:
        return {"scope": "staff", "grades": [], "class_ids": [], "label": "", "parents_only": False, "kind": "staff"}
    if item.audience in (Announcement.Audience.EVERYONE, Announcement.Audience.PARENTS, Announcement.Audience.FAMILIES):
        kind = {"everyone": "school", "parents": "parents", "families": "families"}[item.audience]
        return {"scope": "school", "grades": [], "class_ids": [], "label": "", "parents_only": item.audience == "parents", "kind": kind}
    if item.audience == Announcement.Audience.ROUTE:
        return {"scope": "route", "grades": [], "class_ids": [], "label": item.route.name if item.route else "", "parents_only": False, "kind": "route"}
    groups = list(item.class_groups.all())
    by_grade = by_grade if by_grade is not None else _all_sections()
    grades = sorted({g.grade for g in groups}, key=_grade_key)
    whole = all({x.id for x in by_grade.get(grade, [])} <= {g.id for g in groups} for grade in grades)
    if whole and grades:
        return {"scope": "grades", "grades": grades, "class_ids": [], "label": _range(grades), "parents_only": item.parents_only, "kind": "grades"}
    labels = sorted((g.short_label for g in groups), key=lambda s: (_grade_key(s.rsplit("-", 1)[0]), s))
    return {"scope": "sections", "grades": [], "class_ids": [str(g.id) for g in groups], "label": ", ".join(labels), "parents_only": item.parents_only, "kind": "sections"}


# ------------------------------------------------------------------ read rates


def _reads(items: list[Announcement], school) -> dict:
    """Per announcement: who read it (any channel), and per-channel sent / read counts."""
    ids = [i.id for i in items]
    keys = {f"announcement:{i.id}": i.id for i in items}
    readers: dict = {i: set() for i in ids}
    channels: dict = {i: {} for i in ids}
    notes = Notification.objects.filter(school=school, dedupe_key__in=list(keys)).values("dedupe_key", "user_id", "read_at", "push_status")
    for n in notes:
        aid = keys[n["dedupe_key"]]
        row = channels[aid].setdefault("app", {"sent": 0, "read": 0, "failed": 0})
        row["sent"] += 1
        if n["push_status"] == "failed":
            row["failed"] += 1
        if n["read_at"]:
            row["read"] += 1
            readers[aid].add(n["user_id"])
    for d in ChannelDelivery.objects.filter(announcement_id__in=ids).values("announcement_id", "channel", "status").annotate(n=Count("id")):
        row = channels[d["announcement_id"]].setdefault(d["channel"], {"sent": 0, "read": 0, "failed": 0})
        row["sent"] += d["n"]
        if d["status"] in ("failed", "undelivered"):
            row["failed"] += d["n"]
    for r in AnnouncementRead.objects.filter(announcement_id__in=ids).values("announcement_id", "user_id", "channel"):
        readers[r["announcement_id"]].add(r["user_id"])
        key = "app" if r["channel"] in ("push", "in_app") else r["channel"]
        if key != "app":
            channels[r["announcement_id"]].setdefault(key, {"sent": 0, "read": 0, "failed": 0})["read"] += 1
    for a in AnnouncementAck.objects.filter(announcement_id__in=ids).values("announcement_id", "user_id"):
        readers[a["announcement_id"]].add(a["user_id"])
    return {"readers": readers, "channels": channels}


def _summary(item: Announcement, reads: dict, by_grade: dict) -> dict:
    recipients = item.recipients
    read = len(reads["readers"].get(item.id, ()))
    return {
        "id": str(item.id),
        "title": item.title,
        "kind": item.kind,
        "published_at": item.published_at.isoformat(),
        "scheduled": item.delivered_at is None and item.published_at > timezone.now(),
        "channels": item.channels or ["push", "in_app"],
        "audience": audience_of(item, by_grade),
        "recipients": recipients,
        "read": read,
        "read_rate": _pct(read, recipients),
        "by_channel": reads["channels"].get(item.id, {}),
        "author": item.created_by.full_name if item.created_by else None,
    }


# ------------------------------------------------------------------ circulars and meetings


def _circulars(today, limit: int | None = None) -> list[dict]:
    items = list(Announcement.objects.filter(circular_no__isnull=False).exclude(status=Announcement.Status.DRAFT).select_related("document").order_by("-circular_no"))
    if limit:
        items = items[:limit]
    acks = dict(AnnouncementAck.objects.filter(announcement__in=items).values_list("announcement_id").annotate(n=Count("id")))
    by_grade = _all_sections()
    out = []
    for item in items:
        rate = _pct(acks.get(item.id, 0), item.recipients)
        if rate is not None and rate >= 95:
            state = "done"
        elif item.ack_due_on and item.ack_due_on < today:
            state = "closed"
        else:
            state = "pending"
        out.append(
            {
                "id": str(item.id),
                "number": item.circular_no,
                "title": item.title,
                "published_at": item.published_at.isoformat(),
                "due_on": item.ack_due_on.isoformat() if item.ack_due_on else None,
                "recipients": item.recipients,
                "acknowledged": acks.get(item.id, 0),
                "rate": rate,
                "state": state,
                "audience": audience_of(item, by_grade),
                "document_id": str(item.document_id) if item.document_id else None,
            }
        )
    return out


def _children_label(user) -> list[dict]:
    return [
        {"name": link.student.first_name, "class": link.student.class_group.short_label}
        for link in StudentGuardian.objects.filter(user=user).select_related("student__class_group").order_by("student__class_group__grade")
    ]


def _event_on(day) -> str | None:
    event = Announcement.objects.filter(kind=Announcement.Kind.EVENT, event_starts_at__date=day).exclude(status=Announcement.Status.DRAFT).first()
    return event.title.split(" · ")[0] if event else None


def _meetings(request, statuses=(Meeting.Status.REQUESTED,)) -> list[dict]:
    mine = ConversationMember.objects.filter(user=request.user, side=ConversationMember.Side.STAFF).values("conversation_id")
    meetings = (
        Meeting.objects.filter(conversation_id__in=mine, status__in=statuses, starts_at__gte=timezone.now())
        .select_related("conversation__student__class_group")
        .order_by("starts_at")
    )
    out = []
    events: dict = {}
    for m in meetings:
        family = ConversationMember.objects.filter(conversation=m.conversation, side=ConversationMember.Side.FAMILY).select_related("user").first()
        parent = family.user if family else m.booked_by
        day = timezone.localtime(m.starts_at, school_now(request.school).tzinfo).date()
        if day not in events:
            events[day] = _event_on(day)
        out.append(
            {
                "id": str(m.id),
                "conversation_id": str(m.conversation_id),
                "status": m.status,
                "topic": m.title,
                "starts_at": m.starts_at.isoformat(),
                "ends_at": m.ends_at.isoformat(),
                "location": m.location,
                "event": events[day],
                "parent": {"name": parent.full_name, "initials": parent.initials} if parent else None,
                "children": _children_label(parent) if parent else [],
            }
        )
    return out


def _unread_messages(user) -> int:
    from apps.messaging.views import _unread_counts

    ids = list(ConversationMember.objects.filter(user=user).values_list("conversation_id", flat=True))
    return sum(_unread_counts(user, ids).values())


def sms_credits() -> dict:
    bought = sum(SmsCreditTopUp.objects.values_list("credits", flat=True))
    used = ChannelDelivery.objects.filter(channel="sms").count()
    return {"balance": max(0, bought - used), "bought": bought, "used": used}


def _draft_payload(item: Announcement, by_grade: dict) -> dict:
    return {
        "id": str(item.id),
        "title": item.title,
        "body": item.body,
        "audience": audience_of(item, by_grade),
        "channels": item.channels or ["push", "in_app"],
        "scheduled_at": item.published_at.isoformat() if item.published_at > timezone.now() else None,
        "requires_ack": item.requires_ack,
        "ack_due_on": item.ack_due_on.isoformat() if item.ack_due_on else None,
        "circular": item.circular_no is not None,
        "attachment": {"name": item.attachment_name, "size": item.attachment_size} if item.attachment else None,
        "saved_at": item.updated_at.isoformat(),
    }


# ------------------------------------------------------------------ views


class CommunicationView(SchoolAPIView):
    """Everything the Communication page shows on load."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        today = school_today(request.school)
        by_grade = _all_sections()
        recent = list(
            Announcement.objects.filter(status=Announcement.Status.PUBLISHED, published_at__lte=timezone.now(), circular_no__isnull=True)
            .select_related("created_by", "route")
            .prefetch_related("class_groups")[:4]
        )
        reads = _reads(recent, request.school)
        drafts = list(Announcement.objects.filter(status=Announcement.Status.DRAFT, created_by=request.user).prefetch_related("class_groups").order_by("-updated_at")[:10])
        meetings = _meetings(request)
        quiet = request.school.policy("notifications", "quiet_hours") or ["21:00", "07:00"]
        grades = sorted(by_grade, key=_grade_key)
        return Response(
            {
                "counts": {"messages": _unread_messages(request.user), "meetings": len(meetings)},
                "recent": [_summary(i, reads, by_grade) for i in recent],
                "circulars": _circulars(today, limit=2),
                "meetings": meetings,
                "drafts": [_draft_payload(d, by_grade) for d in drafts],
                "quiet_hours": {"start": quiet[0], "end": quiet[1]},
                "sms_credits": sms_credits(),
                "fields": list(delivery.FIELDS),
                "limit": BODY_LIMIT,
                "grades": [g for g in GRADE_ORDER if g in grades],
                "sections": [
                    {"id": str(g.id), "label": g.short_label, "grade": g.grade}
                    for grade in grades
                    for g in sorted(by_grade[grade], key=lambda x: x.section)
                ],
            }
        )


class AudienceSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(choices=SCOPES)
    grades = serializers.ListField(child=serializers.CharField(max_length=20), required=False, default=list)
    class_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    parents_only = serializers.BooleanField(required=False, default=False)


class EstimateView(SchoolAPIView):
    """Reach per channel for the composer's audience, and the SMS credits it would use."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        data = AudienceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        audience, groups = _audience_for(v["scope"], v["grades"], v["class_ids"], v["parents_only"])
        if audience == Announcement.Audience.CLASSES and not groups:
            return Response({"empty": True, "sms_credits": sms_credits()})
        numbers = delivery.estimate(audience, groups, parents_only=v["parents_only"])
        return Response({**numbers, "empty": False, "sms_credits": sms_credits()})


def _list(request, key) -> list:
    if hasattr(request.data, "getlist"):
        values = request.data.getlist(key)
        if len(values) == 1 and isinstance(values[0], str) and values[0].startswith("["):
            return json.loads(values[0])
        return [v for v in values if v != ""]
    return request.data.get(key) or []


def _flag(request, key) -> bool:
    return str(request.data.get(key, "")).lower() in ("1", "true", "yes", "on")


class ComposeSerializer(serializers.Serializer):
    intent = serializers.ChoiceField(choices=["draft", "send"])
    title = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    body = serializers.CharField(max_length=BODY_LIMIT, required=False, allow_blank=True, default="")
    scope = serializers.ChoiceField(choices=SCOPES, default="school")
    grades = serializers.ListField(child=serializers.CharField(max_length=20), required=False, default=list)
    class_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    parents_only = serializers.BooleanField(default=False)
    channels = serializers.ListField(child=serializers.ChoiceField(choices=delivery.CHANNELS), required=False, default=list)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    circular = serializers.BooleanField(default=False)
    requires_ack = serializers.BooleanField(default=False)
    ack_due_on = serializers.DateField(required=False, allow_null=True, default=None)


class ComposeView(SchoolAPIView):
    """Save a draft, or send / schedule an announcement (a new one, or the draft ``id``)."""

    allowed_roles = CONSOLE_ROLES
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request):
        raw = {k: request.data.get(k) for k in ("intent", "title", "body", "scope", "scheduled_at", "ack_due_on") if request.data.get(k) not in (None, "")}
        raw.update({k: _list(request, k) for k in ("grades", "class_ids", "channels")})
        raw.update({k: _flag(request, k) for k in ("parents_only", "circular", "requires_ack")})
        data = ComposeSerializer(data=raw)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        sending = v["intent"] == "send"
        item = None
        if request.data.get("id"):
            item = Announcement.objects.filter(id=request.data["id"], status=Announcement.Status.DRAFT, created_by=request.user).first()
            if item is None:
                raise Http404
        audience, groups = _audience_for(v["scope"], v["grades"], v["class_ids"], v["parents_only"])
        channels = [c for c in delivery.CHANNELS if c in set(v["channels"]) | {"in_app"}]
        now = timezone.now()
        when = v["scheduled_at"]
        upload = request.FILES.get("attachment")
        if upload is not None and upload.size > 10 * 1024 * 1024:
            raise ValidationError({"attachment": "Attachments can be up to 10 MB."})
        if sending:
            errors = {}
            if not v["title"].strip():
                errors["title"] = "Give the announcement a title."
            if not v["body"].strip():
                errors["body"] = "Write the message."
            if audience == Announcement.Audience.CLASSES and not groups:
                errors["grades"] = "Choose at least one grade or section."
            if when and when < now - timezone.timedelta(minutes=1):
                errors["scheduled_at"] = "Pick a time in the future."
            if v["circular"] and not v["ack_due_on"]:
                errors["ack_due_on"] = "Circulars need an acknowledgement date."
            if errors:
                raise ValidationError(errors)
        item = item or Announcement(created_by=request.user)
        item.title = v["title"].strip()[:120]
        item.body = v["body"]
        item.audience = audience
        item.parents_only = v["parents_only"] and v["scope"] in ("grades", "sections")
        item.channels = channels
        item.requires_ack = v["requires_ack"] or v["circular"]
        item.ack_due_on = v["ack_due_on"]
        if v["circular"] and item.circular_no is None:
            last = Announcement.objects.filter(circular_no__isnull=False).order_by("-circular_no").values_list("circular_no", flat=True).first()
            item.circular_no = (last or 0) + 1 if sending else None
        item.published_at = when if when and when > now else now
        item.status = Announcement.Status.PUBLISHED if sending else Announcement.Status.DRAFT
        if upload is not None:
            item.attachment = upload
            item.attachment_name = upload.name[:120]
            item.attachment_size = upload.size
        elif _flag(request, "remove_attachment"):
            item.attachment, item.attachment_name, item.attachment_size = "", "", 0
        item.save()
        item.class_groups.set(groups)
        counts = {}
        if sending:
            if item.published_at <= now:
                counts = delivery.deliver(item, exclude=request.user)
            else:
                # The count shown until it goes out; deliver() replaces it with who it actually reached.
                r = delivery.resolve(audience, groups, parents_only=item.parents_only)
                item.recipients = len((r["family"] | r["staff"]) - {request.user})
                item.save(update_fields=["recipients", "updated_at"])
            audit(request, "announcements.send", target=item, summary=f"{item.title} · {len(channels)} channels", detail={"channels": channels, "scheduled": item.published_at > now})
        by_grade = _all_sections()
        payload = _draft_payload(item, by_grade) if not sending else _summary(item, _reads([item], request.school), by_grade)
        return Response({**payload, "status": item.status, "delivered": counts}, status=status.HTTP_201_CREATED)


class DraftView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def delete(self, request, announcement_id):
        item = Announcement.objects.filter(id=announcement_id, status=Announcement.Status.DRAFT, created_by=request.user).first()
        if item is None:
            raise Http404
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PreviewView(SchoolAPIView):
    """How the message reads on each channel, for one real recipient in the audience."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        data = AudienceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        title = (request.data.get("title") or "").strip()
        body = request.data.get("body") or ""
        audience, groups = _audience_for(v["scope"], v["grades"], v["class_ids"], v["parents_only"])
        item = Announcement(title=title, body=body, created_by=request.user, school=request.school)
        link = StudentGuardian.objects.filter(student__class_group__in=groups).select_related("user", "student__class_group").first() if groups else None
        if link is None and audience != Announcement.Audience.STAFF:
            link = StudentGuardian.objects.select_related("user", "student__class_group").first()
        user, student = (link.user, link.student) if link else (None, None)
        personal = delivery.render_fields(body, item, user, student)
        sms = delivery.sms_text(item, "eduflow.app/a/xxxxxxxx", personal)
        return Response(
            {
                "sample": {"name": user.full_name, "child": student.first_name, "class": student.class_group.short_label} if user else None,
                "push": {"title": title, "body": " ".join(delivery.plain(delivery.render_fields(body, item)).split())[:180]},
                "sms": {"text": sms, "length": len(sms)},
                "email": {"subject": title, "body": personal},
            }
        )


class ReportsView(SchoolAPIView):
    """Delivery reports: every announcement sent, with reach and reads per channel."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        items = list(
            Announcement.objects.filter(status=Announcement.Status.PUBLISHED)
            .select_related("created_by", "route")
            .prefetch_related("class_groups")
            .order_by("-published_at")[:60]
        )
        reads = _reads(items, request.school)
        by_grade = _all_sections()
        return Response({"items": [{**_summary(i, reads, by_grade), "circular_no": i.circular_no} for i in items], "sms_credits": sms_credits()})


class CircularsView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        return Response({"items": _circulars(school_today(request.school))})


class CircularRemindView(SchoolAPIView):
    """Nudge the families who haven't acknowledged a circular yet (in the app)."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, announcement_id):
        item = Announcement.objects.filter(id=announcement_id, circular_no__isnull=False, status=Announcement.Status.PUBLISHED).first()
        if item is None:
            raise Http404
        r = delivery.resolve(item.audience, list(item.class_groups.all()), item.route, item.parents_only)
        acked = set(AnnouncementAck.objects.filter(announcement=item).values_list("user_id", flat=True))
        pending = [u for u in r["family"] if u.id not in acked]
        stamp = school_today(request.school).isoformat()
        notify(
            pending,
            school=request.school,
            category=Category.ANNOUNCEMENT,
            title=f"Please acknowledge: Circular {item.circular_no}",
            body=item.title,
            data={"announcement_id": str(item.id), "type": "announcement"},
            dedupe_key=f"circular-remind:{item.id}:{stamp}",
        )
        audit(request, "announcements.remind", target=item, summary=f"Circular {item.circular_no} · {len(pending)} reminded")
        return Response({"reminded": len(pending)})


class MeetingsView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        return Response(
            {
                "requested": _meetings(request),
                "booked": _meetings(request, (Meeting.Status.BOOKED,)),
            }
        )


class MeetingDecisionView(SchoolAPIView):
    """Accept a parent's requested slot, or propose another time (the same as the teacher app)."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, meeting_id, action):
        meeting = Meeting.objects.filter(id=meeting_id).select_related("conversation").first()
        if meeting is None or not ConversationMember.objects.filter(conversation=meeting.conversation, user=request.user, side=ConversationMember.Side.STAFF).exists():
            raise Http404
        starts = request.data.get("starts_at")
        if action == "propose" and starts:
            try:
                starts = datetime.fromisoformat(str(starts))
            except ValueError as exc:
                raise ValidationError({"starts_at": "Pick a time."}) from exc
        chat.decide_meeting(meeting, request.user, action, starts)
        return Response(chat.meeting_payload(meeting))


class ConversationsView(SchoolAPIView):
    """Unread total per conversation, for the Messages tab badge (the list itself is /chat/conversations)."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        return Response({"unread": _unread_messages(request.user), "conversations": Conversation.objects.filter(members__user=request.user).count()})


urlpatterns = [
    path("console/communication", CommunicationView.as_view()),
    path("console/communication/estimate", EstimateView.as_view()),
    path("console/communication/preview", PreviewView.as_view()),
    path("console/communication/announcements", ComposeView.as_view()),
    path("console/communication/drafts/<uuid:announcement_id>", DraftView.as_view()),
    path("console/communication/reports", ReportsView.as_view()),
    path("console/communication/circulars", CircularsView.as_view()),
    path("console/communication/circulars/<uuid:announcement_id>/remind", CircularRemindView.as_view()),
    path("console/communication/meetings", MeetingsView.as_view()),
    path("console/communication/meetings/<uuid:meeting_id>/<str:action>", MeetingDecisionView.as_view()),
    path("console/communication/messages", ConversationsView.as_view()),
]

