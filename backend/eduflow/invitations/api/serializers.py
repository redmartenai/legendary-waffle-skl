"""Invitation API contracts. Management responses never contain the secret, its digest, a code or the full
recipient address: only a masked hint."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.identity.api.serializers import AuthSessionOut, MyMembershipOut
from eduflow.identity.delivery import Channel, mask_address
from eduflow.people.api.serializers import PersonRef
from eduflow.people.models import StaffType

from ..models import Invitation, InvitationKind


class RoleRef(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    key = serializers.CharField()
    name = serializers.CharField()


class InvitationOut(serializers.ModelSerializer[Invitation]):
    recipient_hint = serializers.SerializerMethodField()
    roles = RoleRef(many=True)
    student = PersonRef(allow_null=True)
    guardian = PersonRef(allow_null=True)
    department = Ref(allow_null=True)
    campus = Ref(allow_null=True)
    invited_by = serializers.SerializerMethodField()

    class Meta:
        model = Invitation
        fields = (
            "id",
            "kind",
            "status",
            "channel",
            "recipient_hint",
            "full_name",
            "roles",
            "student",
            "guardian",
            "employee_id",
            "staff_type",
            "designation",
            "department",
            "campus",
            "invited_by",
            "expires_at",
            "last_sent_at",
            "send_count",
            "accepted_at",
            "accepted_membership_id",
            "revoked_at",
            "created_at",
        )
        read_only_fields = fields

    def get_recipient_hint(self, obj: Invitation) -> str:
        return mask_address(obj.channel, obj.recipient)

    def get_invited_by(self, obj: Invitation) -> dict[str, str]:
        return {"membership_id": str(obj.invited_by_id), "full_name": obj.invited_by.user.full_name}


class InvitationCreateIn(StrictSerializer):
    kind = serializers.ChoiceField(InvitationKind.choices)
    channel = serializers.ChoiceField(Channel.choices, help_text="Where the link and codes are sent.")
    recipient = serializers.CharField(max_length=254, help_text="Email address or mobile number.")
    full_name = serializers.CharField(
        max_length=200, help_text="Used only if acceptance creates a new account."
    )
    role_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
        max_length=20,
        help_text="Staff only (required). Students and guardians always get their own system role.",
    )
    student_id = serializers.UUIDField(required=False, allow_null=True, help_text="Student invitations.")
    guardian_id = serializers.UUIDField(required=False, allow_null=True, help_text="Guardian invitations.")
    employee_id = serializers.CharField(
        max_length=32, required=False, allow_blank=True, help_text="Staff only."
    )
    staff_type = serializers.ChoiceField(StaffType.choices, required=False, allow_blank=True)
    designation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    campus_id = serializers.UUIDField(required=False, allow_null=True)


class TokenIn(StrictSerializer):
    token = serializers.CharField(max_length=128, help_text="The secret from the invitation link.")


class AcceptIn(TokenIn):
    challenge_id = serializers.UUIDField()
    code = serializers.RegexField(r"^[0-9]{4,8}$", max_length=8)


class InvitationPreviewOut(serializers.Serializer[Any]):
    school_name = serializers.CharField()
    kind = serializers.ChoiceField(InvitationKind.choices)
    role_names = serializers.ListField(child=serializers.CharField())
    channel = serializers.ChoiceField(Channel.choices)
    recipient_hint = serializers.CharField()
    expires_at = serializers.DateTimeField()


class VerificationOut(serializers.Serializer[Any]):
    challenge_id = serializers.UUIDField()
    expires_in = serializers.IntegerField()
    resend_in = serializers.IntegerField()
    dev_code = serializers.CharField(required=False, help_text="Local development only.")


class AcceptanceOut(serializers.Serializer[Any]):
    membership = MyMembershipOut()
    session = AuthSessionOut(
        allow_null=True,
        help_text="Present when acceptance created the account: the new user is signed in. Otherwise null.",
    )
