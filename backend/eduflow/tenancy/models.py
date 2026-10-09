"""Tenancy: the school is the tenant boundary (ADR-003, docs/security/multitenancy.md).

``School`` is the tenant registry. It is platform-owned (a public lookup by code exists), so it has no RLS
policy. ``Membership`` is school-owned and is protected by RLS (tenancy migration 0002).
"""

from __future__ import annotations

import zoneinfo
from typing import Any, TypeVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from eduflow.core.ids import uuid7

M = TypeVar("M", bound=models.Model)


class TenantQuerySet(models.QuerySet[M]):
    def for_school(self, school: School | Any) -> TenantQuerySet[M]:
        """Every read of school-owned data starts here (``Model.objects.for_school(tenant)``)."""
        school_id = school.pk if isinstance(school, School) else school
        if school_id is None:
            raise ValueError("A school is required.")
        return self.filter(school_id=school_id)


class TenantModel(models.Model):
    """Base for every school-owned table: a non-null, indexed school key (docs/database/conventions.md)."""

    school = models.ForeignKey("tenancy.School", on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError("Use an IANA time zone name, e.g. Asia/Kolkata.")


class Address(models.Model):
    """Postal address fields shared by schools and campuses. ``country`` is an ISO 3166-1 alpha-2 code."""

    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, blank=True, default="IN")

    class Meta:
        abstract = True


class School(Address):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    code = models.SlugField(
        max_length=32, unique=True, help_text="Short public code used to find the school."
    )
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True, help_text="Registered name, if different.")
    short_name = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    website = models.URLField(blank=True)
    timezone = models.CharField(max_length=64, default="Asia/Kolkata", validators=[validate_timezone])
    # School-level preferences (e.g. naming conventions). Never credentials or personal data.
    settings = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    # Bumped whenever roles, role permissions or role assignments change, so cached permission sets for
    # this school become stale at once (eduflow.authz.grants).
    rbac_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenancy_school"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[a-z0-9][a-z0-9-]{1,31}$"), name="tenancy_school_code_check"
            ),
        ]

    def __str__(self) -> str:
        return self.code


class Membership(TenantModel):
    """A user's place in one school. Roles hang off it (``authz.MembershipRole``)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "tenancy_membership"
        constraints = [
            models.UniqueConstraint(fields=["school", "user"], name="tenancy_membership_school_user_uniq"),
            # Target of composite foreign keys from authz tables, so a child row's school always matches.
            models.UniqueConstraint(fields=["id", "school"], name="tenancy_membership_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["user", "is_active"], name="tenancy_membership_user_idx")]

    def __str__(self) -> str:
        return str(self.id)
