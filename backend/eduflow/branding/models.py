"""White-label branding: a school's colours, logo and favicon, and its custom domains (ADR-029).

* The school's **name** stays where it is (``tenancy.School.name`` / ``short_name``); branding never copies
  it.
* **Assets** are images validated by content (images.py) and stored privately in object storage (ADR-009).
  They are served by the API under their immutable ID, never by storage path.
* **Domains**: a school's platform subdomain (``<code>.<base domain>``) is derived from its immutable code and
  needs no row. A **custom** domain is a row here, usable only once its owner has proven control (DNS TXT).

All three tables are school-owned and under RLS. Public, pre-authentication reads go through the named,
logged bypass in selectors.py and return public fields only.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from eduflow.core.ids import uuid7
from eduflow.tenancy.models import Membership, School, TenantModel, TenantQuerySet

HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"


def _same_school_target(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"branding_{model}_id_school_uniq")


class AssetKind(models.TextChoices):
    LOGO = "logo", "Logo"
    FAVICON = "favicon", "Favicon"


class BrandAsset(TenantModel):
    """An uploaded image. Immutable: replacing a logo uploads a new asset."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    kind = models.CharField(max_length=16, choices=AssetKind.choices)
    content_type = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(
        max_length=255, unique=True, help_text="Internal; never returned by the API."
    )
    uploaded_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "branding_asset"
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=AssetKind.values), name="branding_asset_kind_check"),
            models.CheckConstraint(
                condition=Q(content_type__in=["image/png", "image/jpeg", "image/webp", "image/x-icon"]),
                name="branding_asset_content_type_check",
            ),
            _same_school_target("asset"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


class SchoolBranding(TenantModel):
    """One per school (created on first change). Without a row, the platform defaults apply."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    school = models.OneToOneField(School, on_delete=models.PROTECT, related_name="branding")
    primary_color = models.CharField(max_length=7, blank=True, help_text="#RRGGBB; empty = platform default.")
    secondary_color = models.CharField(
        max_length=7, blank=True, help_text="#RRGGBB; empty = platform default."
    )
    logo = models.ForeignKey(BrandAsset, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    favicon = models.ForeignKey(BrandAsset, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    version = models.PositiveIntegerField(default=1, help_text="Bumped on every change; part of the ETag.")
    updated_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "branding_school_branding"
        constraints = [
            models.CheckConstraint(
                condition=Q(primary_color="") | Q(primary_color__regex=HEX_COLOR),
                name="branding_primary_color_check",
            ),
            models.CheckConstraint(
                condition=Q(secondary_color="") | Q(secondary_color__regex=HEX_COLOR),
                name="branding_secondary_color_check",
            ),
        ]

    def __str__(self) -> str:
        return f"branding:{self.school_id}"


class DomainStatus(models.TextChoices):
    PENDING = "pending", "Pending verification"
    VERIFIED = "verified", "Verified"
    DISABLED = "disabled", "Disabled"


class SchoolDomain(TenantModel):
    """A custom hostname (``portal.greenvalley.edu.in``). It resolves to the school only while ``verified``.

    Lifecycle: ``pending`` --(DNS TXT proof)--> ``verified``; ``verified`` --(owner or platform, or lapsed
    proof)--> ``disabled``; a school may re-verify a domain it disabled, but only the platform can lift a
    platform suspension. One hostname belongs to at most one school, platform-wide.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hostname = models.CharField(max_length=253, help_text="Lower-case, ASCII (IDNA) form.")
    status = models.CharField(max_length=16, choices=DomainStatus.choices, default=DomainStatus.PENDING)
    is_primary = models.BooleanField(default=False)
    verification_token = models.CharField(
        max_length=64, help_text="Published by the owner in DNS; not a secret."
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    check_failures = models.PositiveSmallIntegerField(default=0)
    disabled_reason = models.CharField(max_length=32, blank=True)
    suspended_by_platform = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "branding_domain"
        constraints = [
            models.UniqueConstraint(Lower("hostname"), name="branding_domain_hostname_uniq"),
            models.CheckConstraint(
                condition=Q(hostname=Lower("hostname")), name="branding_domain_lowercase_check"
            ),
            models.CheckConstraint(
                condition=Q(status__in=DomainStatus.values), name="branding_domain_status_check"
            ),
            models.CheckConstraint(
                condition=~Q(status=DomainStatus.VERIFIED) | Q(verified_at__isnull=False),
                name="branding_domain_verified_at_check",
            ),
            models.CheckConstraint(
                condition=Q(is_primary=False) | Q(status=DomainStatus.VERIFIED),
                name="branding_domain_primary_is_verified_check",
            ),
            models.UniqueConstraint(
                fields=["school"], condition=Q(is_primary=True), name="branding_domain_one_primary_uniq"
            ),
            _same_school_target("domain"),
        ]
        indexes = [models.Index(fields=["status"], name="branding_domain_status_idx")]

    def __str__(self) -> str:
        return self.hostname
