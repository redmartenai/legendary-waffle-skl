"""Branding serializers. Public responses carry only public fields (see selectors.payload)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from eduflow.core.api import StrictSerializer

from .. import verification
from ..models import HEX_COLOR, DomainStatus, SchoolDomain


class BrandSchoolOut(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    short_name = serializers.CharField(allow_blank=True)


class BrandingOut(serializers.Serializer[Any]):
    school = BrandSchoolOut(allow_null=True, help_text="Null for the platform's own branding.")
    display_name = serializers.CharField()
    primary_color = serializers.CharField(help_text="#RRGGBB")
    secondary_color = serializers.CharField(help_text="#RRGGBB")
    on_primary = serializers.CharField(help_text="Black or white text colour with the best contrast.")
    on_secondary = serializers.CharField()
    logo_url = serializers.CharField(allow_null=True, help_text="An API path; null when there is no logo.")
    favicon_url = serializers.CharField(allow_null=True)
    version = serializers.IntegerField(help_text="Changes with every branding change (cache key).")
    is_default = serializers.BooleanField(help_text="True while the school uses the platform defaults.")


class ResolvedBrandingOut(BrandingOut):
    host_kind = serializers.ChoiceField(["platform", "subdomain", "custom"])


class ResolveQuery(StrictSerializer):
    host = serializers.CharField(
        max_length=253,
        required=False,
        help_text="The hostname the client is served on. Defaults to this request's.",
    )


class ColorsIn(StrictSerializer):
    primary_color = serializers.RegexField(HEX_COLOR, required=False, allow_blank=True)
    secondary_color = serializers.RegexField(HEX_COLOR, required=False, allow_blank=True)


class AssetUploadIn(StrictSerializer):
    file = serializers.FileField(help_text="PNG, JPEG or WebP (logo); PNG or ICO (favicon). No SVG.")


class VerificationRecordOut(serializers.Serializer[Any]):
    type = serializers.CharField()
    name = serializers.CharField()
    value = serializers.CharField()


class DomainOut(serializers.ModelSerializer[SchoolDomain]):
    verification = serializers.SerializerMethodField()

    class Meta:
        model = SchoolDomain
        fields = (
            "id",
            "hostname",
            "status",
            "is_primary",
            "verified_at",
            "last_checked_at",
            "disabled_reason",
            "suspended_by_platform",
            "verification",
            "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(VerificationRecordOut(allow_null=True))
    def get_verification(self, obj: SchoolDomain) -> dict[str, str] | None:
        if obj.status == DomainStatus.VERIFIED:
            return None
        return {
            "type": "TXT",
            "name": verification.challenge_name(obj.hostname),
            "value": verification.challenge_value(obj.verification_token),
        }


class DomainIn(StrictSerializer):
    hostname = serializers.CharField(max_length=253)


class PlatformVerifyIn(StrictSerializer):
    note = serializers.CharField(max_length=300, help_text="How ownership was confirmed (audited).")


class DisableIn(StrictSerializer):
    reason = serializers.CharField(max_length=32, required=False, allow_blank=True)
