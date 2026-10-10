"""Notification serializers: a person's own notifications and channel preferences."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer

from ..models import Channel, Notification, NotificationKind


class NotificationOut(serializers.ModelSerializer[Notification]):
    student_id = serializers.UUIDField(allow_null=True)
    read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = (
            "id",
            "kind",
            "title",
            "body",
            "student_id",
            "link_type",
            "link_id",
            "read",
            "read_at",
            "created_at",
        )
        read_only_fields = fields

    def get_read(self, obj: Notification) -> bool:
        return obj.read_at is not None


class ReadIn(StrictSerializer):
    ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        max_length=500,
        help_text="Omit to mark everything read.",
    )


class PreferenceItem(serializers.Serializer[Any]):
    kind = serializers.ChoiceField(NotificationKind.choices)
    channel = serializers.ChoiceField(Channel.choices)
    enabled = serializers.BooleanField()


class PreferencesIn(StrictSerializer):
    preferences = PreferenceItem(many=True)


class PreferencesOut(serializers.Serializer[Any]):
    preferences = PreferenceItem(many=True)
