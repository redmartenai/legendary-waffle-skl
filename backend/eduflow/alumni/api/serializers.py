from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer

from ..models import Alumnus, Campaign, Donation, Event, Registration


class AlumnusOut(serializers.ModelSerializer[Alumnus]):
    student_id = serializers.UUIDField(allow_null=True)

    class Meta:
        model = Alumnus
        fields = (
            "id",
            "student_id",
            "full_name",
            "graduation_year",
            "email",
            "phone",
            "occupation",
            "organisation",
            "city",
            "consent_to_contact",
            "created_at",
        )
        read_only_fields = fields


class AlumnusIn(StrictSerializer):
    student_id = serializers.UUIDField(required=False, allow_null=True)
    full_name = serializers.CharField(max_length=200, required=False)
    graduation_year = serializers.IntegerField(min_value=1900, max_value=2200)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    occupation = serializers.CharField(max_length=150, required=False, allow_blank=True)
    organisation = serializers.CharField(max_length=150, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    consent_to_contact = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("student_id") and not attrs.get("full_name"):
            raise serializers.ValidationError({"full_name": ["Give a name or link a student."]})
        return attrs


class AlumnusUpdateIn(StrictSerializer):
    full_name = serializers.CharField(max_length=200, required=False)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    occupation = serializers.CharField(max_length=150, required=False, allow_blank=True)
    organisation = serializers.CharField(max_length=150, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    consent_to_contact = serializers.BooleanField(required=False)


class EventOut(serializers.ModelSerializer[Event]):
    registered = serializers.IntegerField(read_only=True)

    class Meta:
        model = Event
        fields = ("id", "title", "starts_at", "venue", "description", "capacity", "registered")
        read_only_fields = fields


class EventIn(StrictSerializer):
    title = serializers.CharField(max_length=200)
    starts_at = serializers.DateTimeField()
    venue = serializers.CharField(max_length=200, required=False, allow_blank=True)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    capacity = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class RegistrationIn(StrictSerializer):
    alumnus_id = serializers.UUIDField()
    guests = serializers.IntegerField(min_value=0, max_value=10, required=False, default=0)


class RegistrationOut(serializers.ModelSerializer[Registration]):
    alumnus = AlumnusOut()

    class Meta:
        model = Registration
        fields = ("id", "alumnus", "guests", "created_at")
        read_only_fields = fields


class CampaignOut(serializers.ModelSerializer[Campaign]):
    raised = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Campaign
        fields = ("id", "title", "purpose", "goal_amount", "starts_on", "ends_on", "raised")
        read_only_fields = fields


class CampaignIn(StrictSerializer):
    title = serializers.CharField(max_length=200)
    purpose = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    goal_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    starts_on = serializers.DateField()
    ends_on = serializers.DateField()


class DonationOut(serializers.ModelSerializer[Donation]):
    campaign_id = serializers.UUIDField()
    alumnus_id = serializers.UUIDField(allow_null=True)

    class Meta:
        model = Donation
        fields = (
            "id",
            "campaign_id",
            "alumnus_id",
            "donor_name",
            "amount",
            "received_on",
            "mode",
            "reference",
            "receipt_number",
        )
        read_only_fields = fields


class DonationIn(StrictSerializer):
    alumnus_id = serializers.UUIDField(required=False, allow_null=True)
    donor_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    received_on = serializers.DateField()
    mode = serializers.CharField(max_length=30)
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True)
