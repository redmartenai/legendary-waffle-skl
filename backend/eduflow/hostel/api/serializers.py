from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import Allocation, Hostel, Outpass, RollCall, RollStatus, Room


class HostelRoomOut(serializers.ModelSerializer[Room]):
    occupied = serializers.IntegerField(read_only=True, default=None)

    class Meta:
        model = Room
        fields = ("id", "number", "beds", "occupied")
        read_only_fields = fields


class HostelRoomIn(StrictSerializer):
    number = serializers.CharField(max_length=20)
    beds = serializers.IntegerField(min_value=1, max_value=50)


class HostelOut(serializers.ModelSerializer[Hostel]):
    warden = serializers.CharField(source="warden.user.full_name", allow_null=True, default=None)
    rooms = HostelRoomOut(many=True)

    class Meta:
        model = Hostel
        fields = ("id", "name", "warden", "rooms")
        read_only_fields = fields


class HostelIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    warden_id = serializers.UUIDField(required=False, allow_null=True)
    rooms = HostelRoomIn(many=True, required=False)


class AllocationOut(serializers.ModelSerializer[Allocation]):
    student = PersonRef()
    hostel = serializers.CharField(source="room.hostel.name")
    room = serializers.CharField(source="room.number")

    class Meta:
        model = Allocation
        fields = ("id", "student", "hostel", "room", "start_date", "end_date")
        read_only_fields = fields


class AllocationIn(StrictSerializer):
    student_id = serializers.UUIDField()
    room_id = serializers.UUIDField()
    start_date = serializers.DateField(required=False)


class OutpassOut(serializers.ModelSerializer[Outpass]):
    student = PersonRef()

    class Meta:
        model = Outpass
        fields = (
            "id",
            "student",
            "leave_at",
            "return_by",
            "reason",
            "status",
            "decided_at",
            "decision_note",
            "checked_out_at",
            "returned_at",
            "created_at",
        )
        read_only_fields = fields


class OutpassIn(StrictSerializer):
    student_id = serializers.UUIDField()
    leave_at = serializers.DateTimeField()
    return_by = serializers.DateTimeField()
    reason = serializers.CharField(max_length=500)


class GateIn(StrictSerializer):
    action = serializers.ChoiceField(["check_out", "check_in"])


class RollEntryIn(StrictSerializer):
    student_id = serializers.UUIDField()
    status = serializers.ChoiceField(RollStatus.choices)


class RollIn(StrictSerializer):
    date = serializers.DateField()
    entries = RollEntryIn(many=True, allow_empty=False)


class RollOut(serializers.ModelSerializer[RollCall]):
    student = PersonRef()

    class Meta:
        model = RollCall
        fields = ("id", "student", "date", "status")
        read_only_fields = fields


class RecordedOut(serializers.Serializer[Any]):
    recorded = serializers.IntegerField()
