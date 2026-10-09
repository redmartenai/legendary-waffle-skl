"""RBAC write operations. Every change is audited and bumps the school's ``rbac_version``.

Escalation guards (docs/security/authorization.md#escalation):

* To grant a permission to a role, or to give someone a role, the actor must hold each of the role's
  permissions with ``school`` scope themselves. Nobody can hand out more than they have.
* The ``school_admin`` role cannot be edited or deleted, and the last active school admin cannot lose it.
* System roles cannot be deleted. Custom roles cannot be deleted while assigned.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils.text import slugify
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.audit import services as audit
from eduflow.core.api import Conflict
from eduflow.core.ids import uuid7

from .catalog import ADMIN_ROLE, LOCKED_ROLES, PERMISSIONS, SYSTEM_ROLES, DataScope
from .grants import Actor
from .models import MembershipRole, Permission, Role, RolePermission

if TYPE_CHECKING:
    from eduflow.tenancy.models import Membership, School

GrantSpec = Mapping[str, Iterable[str]]


def sync_permission_catalog(using: str = "default") -> None:
    """Mirror ``catalog.PERMISSIONS`` into the table. Removed codenames are marked deprecated, not deleted."""
    for codename, description in PERMISSIONS.items():
        Permission.objects.using(using).update_or_create(
            codename=codename, defaults={"description": description, "is_deprecated": False}
        )
    Permission.objects.using(using).exclude(codename__in=list(PERMISSIONS)).update(is_deprecated=True)


def bump_rbac_version(school_id: Any) -> None:
    from eduflow.tenancy.models import School

    School.objects.filter(pk=school_id).update(rbac_version=F("rbac_version") + 1)


def _validate_grants(spec: GrantSpec) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    clean: dict[str, list[str]] = {}
    valid_scopes = set(DataScope.values) - {DataScope.PLATFORM}
    for codename, scopes in spec.items():
        scope_list = sorted(set(scopes))
        if codename not in PERMISSIONS:
            errors[codename] = ["Unknown permission."]
        elif not scope_list or not set(scope_list) <= valid_scopes:
            errors[codename] = ["Choose one or more valid data scopes."]
        else:
            clean[codename] = scope_list
    if errors:
        raise ValidationError({"permissions": errors})
    return clean


def _ensure_can_grant(actor: Actor | None, codenames: Iterable[str]) -> None:
    if actor is None:  # system/platform seeding
        return
    missing = sorted(c for c in codenames if DataScope.SCHOOL not in actor.scopes(c))
    if missing:
        raise PermissionDenied("You can only grant permissions you hold for the whole school.")


def _set_grants(role: Role, grants: Mapping[str, list[str]]) -> None:
    RolePermission.objects.filter(role=role).delete()
    RolePermission.objects.bulk_create(
        RolePermission(school_id=role.school_id, role=role, permission_id=c, scopes=s)
        for c, s in grants.items()
    )


def seed_system_roles(school: School, *, only_missing_grants: bool = False) -> list[Role]:
    """Create the default roles for a school (or, for existing schools, add new default grants only)."""
    roles = []
    for key, (name, grants) in SYSTEM_ROLES.items():
        role, created = Role.objects.get_or_create(
            school=school, key=key, defaults={"name": name, "is_system": True}
        )
        spec = {c: [str(s) for s in scopes] for c, scopes in grants.items()}
        if created or not only_missing_grants:
            _set_grants(role, spec)
        else:
            existing = set(RolePermission.objects.filter(role=role).values_list("permission_id", flat=True))
            RolePermission.objects.bulk_create(
                RolePermission(school_id=school.pk, role=role, permission_id=c, scopes=s)
                for c, s in spec.items()
                if c not in existing
            )
        roles.append(role)
    bump_rbac_version(school.pk)
    return roles


def ensure_can_assign(actor: Actor, role: Role) -> None:
    """Raise ``PermissionDenied`` unless ``actor`` may give ``role`` (holds all of it school-wide)."""
    _ensure_can_grant(actor, role_grants(role))


def role_grants(role: Role) -> dict[str, list[str]]:
    return {r.permission_id: sorted(r.scopes) for r in RolePermission.objects.filter(role=role)}


@transaction.atomic
def create_role(actor: Actor, *, name: str, grants: GrantSpec, based_on: Role | None = None) -> Role:
    spec = dict(grants) if grants else (role_grants(based_on) if based_on else {})
    clean = _validate_grants(spec)
    _ensure_can_grant(actor, clean)
    key = f"custom-{slugify(name)[:24] or 'role'}-{uuid7().hex[-6:]}"
    role = Role.objects.create(school=actor.school, key=key, name=name, is_system=False, based_on=based_on)
    _set_grants(role, clean)
    bump_rbac_version(actor.school.pk)
    audit.record(
        "authz.role.created",
        target_type="role",
        target_id=role.id,
        metadata={"name": name, "based_on": str(based_on.id) if based_on else None, "permissions": clean},
    )
    return role


@transaction.atomic
def update_role(
    actor: Actor, role: Role, *, name: str | None = None, grants: GrantSpec | None = None
) -> Role:
    if role.key in LOCKED_ROLES:
        raise PermissionDenied("This role is locked.")
    before = role_grants(role)
    if name is not None:
        role.name = name
        role.save(update_fields=["name", "updated_at"])
    after = before
    if grants is not None:
        after = _validate_grants(grants)
        changed = {c for c in set(before) | set(after) if before.get(c) != after.get(c)}
        _ensure_can_grant(actor, changed)
        _set_grants(role, after)
    bump_rbac_version(actor.school.pk)
    audit.record(
        "authz.role.updated",
        target_type="role",
        target_id=role.id,
        metadata={
            "name": role.name,
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "changed_scopes": sorted(c for c in set(before) & set(after) if before[c] != after[c]),
        },
    )
    return role


@transaction.atomic
def delete_role(actor: Actor, role: Role) -> None:
    if role.is_system:
        raise PermissionDenied("System roles cannot be deleted.")
    if MembershipRole.objects.filter(role=role).exists():
        raise Conflict("Remove this role from its members before deleting it.")
    _ensure_can_grant(actor, role_grants(role))
    role_id = role.id
    role.delete()
    bump_rbac_version(actor.school.pk)
    audit.record("authz.role.deleted", target_type="role", target_id=role_id, metadata={"name": role.name})


def assign_role(
    actor: Actor | None, membership: Membership, role: Role, *, title: str = "", department: str = ""
) -> MembershipRole:
    if role.school_id != membership.school_id:
        raise ValidationError({"role_id": ["Unknown role."]})
    _ensure_can_grant(actor, role_grants(role))
    try:
        with transaction.atomic():
            assignment = MembershipRole.objects.create(
                school_id=membership.school_id,
                membership=membership,
                role=role,
                title=title,
                department=department,
            )
            bump_rbac_version(membership.school_id)
            audit.record(
                "authz.role.assigned",
                actor_id=actor.user.pk if actor else None,
                school_id=membership.school_id,
                target_type="membership",
                target_id=membership.id,
                metadata={"role": role.key, "user_id": str(membership.user_id)},
            )
    except IntegrityError:
        raise Conflict("The member already has this role.") from None
    return assignment


def lock_school(school_id: Any) -> None:
    """Serialise admin-count checks for one school (prevents two admins demoting each other at once)."""
    from eduflow.tenancy.models import School

    School.objects.select_for_update().filter(pk=school_id).values_list("pk", flat=True).first()


def ensure_can_manage_roles_of(actor: Actor, membership: Membership) -> None:
    codenames: set[str] = set()
    for role in Role.objects.filter(assignments__membership=membership):
        codenames.update(role_grants(role))
    _ensure_can_grant(actor, codenames)


def _active_admin_count(school_id: Any) -> int:
    return (
        MembershipRole.objects.filter(
            school_id=school_id,
            role__key=ADMIN_ROLE,
            membership__is_active=True,
            membership__user__is_active=True,
        )
        .values("membership_id")
        .distinct()
        .count()
    )


@transaction.atomic
def unassign_role(actor: Actor, assignment: MembershipRole) -> None:
    role = assignment.role
    _ensure_can_grant(actor, role_grants(role))
    lock_school(assignment.school_id)
    if role.key == ADMIN_ROLE and _active_admin_count(assignment.school_id) <= 1:
        raise Conflict("A school must keep at least one active school admin.")
    membership_id = assignment.membership_id
    assignment.delete()
    bump_rbac_version(assignment.school_id)
    audit.record(
        "authz.role.unassigned",
        target_type="membership",
        target_id=membership_id,
        metadata={"role": role.key},
    )


def is_last_admin(membership: Membership) -> bool:
    holds_admin = MembershipRole.objects.filter(membership=membership, role__key=ADMIN_ROLE).exists()
    return holds_admin and _active_admin_count(membership.school_id) <= 1
