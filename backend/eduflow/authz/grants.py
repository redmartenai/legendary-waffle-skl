"""Effective permissions: what a member may do in a school, and over which data scopes.

The effective grant for a permission is the union of the scopes given by each of the member's roles. The
result is cached in Redis per (membership, school RBAC version), so any role, grant or assignment change
(which bumps ``School.rbac_version``) invalidates it at once. Cache entries only ever hold codenames and
scope names.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.core.cache import cache

from eduflow.core.logging import get_logger

from .catalog import DataScope
from .models import RolePermission

if TYPE_CHECKING:
    from eduflow.identity.models import User
    from eduflow.tenancy.models import Membership, School

Grants = Mapping[str, frozenset[DataScope]]

_CACHE_SECONDS = 300

log = get_logger(__name__)


def _cache_key(membership: Membership, school: School) -> str:
    return f"authz:grants:{membership.pk}:{school.rbac_version}"


def compute_grants(membership: Membership) -> dict[str, frozenset[DataScope]]:
    if not membership.is_active:
        return {}
    union: dict[str, set[DataScope]] = defaultdict(set)
    rows = RolePermission.objects.for_school(membership.school_id).filter(
        role__assignments__membership=membership, permission__is_deprecated=False
    )
    for codename, scopes in rows.values_list("permission_id", "scopes"):
        union[codename].update(DataScope(s) for s in scopes)
    return {codename: frozenset(scopes) for codename, scopes in union.items()}


def grants_for(membership: Membership, school: School) -> dict[str, frozenset[DataScope]]:
    """Cached effective grants. The cache is only an optimisation: if Redis fails, the database answers."""
    key = _cache_key(membership, school)
    try:
        cached = cache.get(key)
    except Exception:  # any cache error: fall back to the authoritative database
        log.warning("grants_cache_unavailable")
        return compute_grants(membership)
    if cached is not None:
        return {k: frozenset(DataScope(s) for s in v) for k, v in cached.items()}
    grants = compute_grants(membership)
    try:
        cache.set(key, {k: sorted(v) for k, v in grants.items()}, _CACHE_SECONDS)
    except Exception:
        log.warning("grants_cache_unavailable")
    return grants


@dataclass(frozen=True)
class Actor:
    """The caller of a tenant-scoped request: who, in which school, allowed to do what."""

    user: User
    school: School
    membership: Membership
    grants: Grants = field(default_factory=dict)

    def has(self, permission: str) -> bool:
        return bool(self.grants.get(permission))

    def scopes(self, permission: str) -> frozenset[DataScope]:
        return self.grants.get(permission, frozenset())
