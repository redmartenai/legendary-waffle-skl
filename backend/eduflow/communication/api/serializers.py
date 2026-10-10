from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import (
    Announcement,
    AnnouncementResponse,
    Audience,
    Complaint,
    ComplaintCategory,
    ComplaintStatus,
    Message,
    Sentiment,
    Thread,
)


class ResponseOut(serializers.ModelSerializer[AnnouncementResponse]):
    member = serializers.CharField(source="member.user.full_name")

    class Meta:
        model = AnnouncementResponse
        fields = ("id", "member", "text", "at")
        read_only_fields = fields


class AnnouncementOut(serializers.ModelSerializer[Announcement]):
    section = Ref(allow_null=True)
    author = serializers.CharField(source="author.user.full_name", allow_null=True, default=None)
    audiences = serializers.ListField(child=serializers.ChoiceField(Audience.choices))
    responses = ResponseOut(many=True)

    class Meta:
        model = Announcement
        fields = (
            "id",
            "title",
            "body",
            "audiences",
            "section",
            "pinned",
            "requires_acknowledgement",
            "author",
            "responses",
            "created_at",
            "archived_at",
        )
        read_only_fields = fields


class AnnouncementIn(StrictSerializer):
    title = serializers.CharField(max_length=200)
    body = serializers.CharField(max_length=10000)
    audiences = serializers.ListField(
        child=serializers.ChoiceField(Audience.choices), min_length=1, max_length=3
    )
    section_id = serializers.UUIDField(required=False, allow_null=True)
    pinned = serializers.BooleanField(required=False, default=False)
    requires_acknowledgement = serializers.BooleanField(required=False, default=True)


class AnnouncementUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=200, required=False)
    body = serializers.CharField(max_length=10000, required=False)
    pinned = serializers.BooleanField(required=False)


class RespondIn(StrictSerializer):
    text = serializers.CharField(max_length=2000)


class MemberRef(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    full_name = serializers.CharField(source="user.full_name")


class AckStatusOut(serializers.Serializer[Any]):
    recipients = serializers.IntegerField()
    acknowledged = serializers.IntegerField()
    pending = MemberRef(many=True)


class ThreadOut(serializers.ModelSerializer[Thread]):
    student = PersonRef()
    staff = serializers.CharField(source="staff.membership.user.full_name")
    guardian = serializers.CharField(source="guardian.full_name", allow_null=True, default=None)
    subject = Ref(allow_null=True)
    awaiting_reply = serializers.SerializerMethodField()

    class Meta:
        model = Thread
        fields = (
            "id",
            "kind",
            "student",
            "staff",
            "guardian",
            "subject",
            "awaiting_reply",
            "awaiting_reply_since",
            "last_message_at",
        )
        read_only_fields = fields

    def get_awaiting_reply(self, thread: Thread) -> bool:
        return thread.awaiting_reply_since is not None


class ThreadIn(StrictSerializer):
    student_id = serializers.UUIDField()
    staff_id = serializers.UUIDField(required=False, allow_null=True)
    guardian_id = serializers.UUIDField(required=False, allow_null=True)
    subject_id = serializers.UUIDField(required=False, allow_null=True)


class MessageOut(serializers.ModelSerializer[Message]):
    author = serializers.CharField(source="author.user.full_name")

    class Meta:
        model = Message
        fields = ("id", "author", "from_staff", "text", "at")
        read_only_fields = fields


class MessageIn(StrictSerializer):
    text = serializers.CharField(max_length=5000)


class ComplaintOut(serializers.ModelSerializer[Complaint]):
    student = PersonRef()
    raised_by = serializers.CharField(source="raised_by.user.full_name")
    assigned_to = serializers.CharField(source="assigned_to.user.full_name", allow_null=True, default=None)

    class Meta:
        model = Complaint
        fields = (
            "id",
            "student",
            "category",
            "text",
            "sentiment",
            "status",
            "raised_by",
            "assigned_to",
            "resolution",
            "resolved_at",
            "created_at",
        )
        read_only_fields = fields


class ComplaintIn(StrictSerializer):
    student_id = serializers.UUIDField()
    category = serializers.ChoiceField(ComplaintCategory.choices)
    text = serializers.CharField(max_length=3000)
    sentiment = serializers.ChoiceField(Sentiment.choices, help_text="Chosen by the reporter.")


class ComplaintUpdateIn(StrictSerializer):
    status = serializers.ChoiceField(ComplaintStatus.choices, required=False)
    assigned_to_id = serializers.UUIDField(required=False, allow_null=True)
    resolution = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    sentiment = serializers.ChoiceField(Sentiment.choices, required=False)


class SentimentOut(serializers.Serializer[Any]):
    total = serializers.IntegerField()
    by_sentiment = serializers.DictField(child=serializers.IntegerField())
    by_category = serializers.DictField(child=serializers.DictField(child=serializers.IntegerField()))
    net_sentiment = serializers.FloatField(allow_null=True, help_text="(positive - negative) / total")
