"""RBAC tables: User -> Membership -> Role -> Permission (+ data scope) (ADR-004).

``Permission`` is the platform-wide catalogue. ``Role``, ``RolePermission`` and ``MembershipRole`` are
school-owned: each carries ``school_id``, is protected by RLS, and composite foreign keys (authz migration
0002) make it impossible at the database level to attach one school's role to another school's member.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from eduflow.core.ids import uuid7
from eduflow.tenancy.models import TenantModel, TenantQuerySet

from .catalog import DataScope


class Permission(models.Model):
    codename = models.CharField(max_length=64, primary_key=True)
    description = models.CharField(max_length=200)
    # Kept (not deleted) when removed from the catalogue, so existing grants stay explainable.
    is_deprecated = models.BooleanField(default=False)

    class Meta:
        db_table = "authz_permission"

    def __str__(self) -> str:
        return self.codename


class Role(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    key = models.SlugField(max_length=48, help_text="Stable machine key, unique within the school.")
    name = models.CharField(max_length=100)
    is_system = models.BooleanField(default=False)
    based_on = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "authz_role"
        constraints = [
            models.UniqueConstraint(fields=["school", "key"], name="authz_role_school_key_uniq"),
            models.UniqueConstraint(fields=["id", "school"], name="authz_role_id_school_uniq"),
        ]

    def __str__(self) -> str:
        return self.key


class RolePermission(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="grants")
    permission = models.ForeignKey(Permission, on_delete=models.PROTECT, related_name="+")
    scopes = ArrayField(models.CharField(max_length=16, choices=DataScope.choices), size=len(DataScope))

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "authz_role_permission"
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="authz_role_permission_uniq"),
            models.CheckConstraint(
                condition=models.Q(scopes__len__gt=0), name="authz_role_permission_scopes_check"
            ),
        ]
        indexes = [models.Index(fields=["school", "role"], name="authz_role_permission_role_idx")]


class MembershipRole(TenantModel):
    """A role given to a member, with an optional title and department (e.g. "Class teacher", "Science")."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    membership = models.ForeignKey(
        "tenancy.Membership", on_delete=models.CASCADE, related_name="role_assignments"
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="assignments")
    title = models.CharField(max_length=100, blank=True)
    department = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "authz_membership_role"
        constraints = [
            models.UniqueConstraint(fields=["membership", "role"], name="authz_membership_role_uniq"),
        ]
        indexes = [models.Index(fields=["school", "membership"], name="authz_membership_role_idx")]
