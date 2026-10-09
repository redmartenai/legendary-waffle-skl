"""Tenancy reads. School-owned queries go through a ScopedResource, which applies tenant and data scope."""

from __future__ import annotations

import uuid

from django.db.models import Prefetch, Q, QuerySet

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.models import MembershipRole
from eduflow.authz.scopes import ScopedResource
from eduflow.identity.models import User

from .models import Membership, School

members: ScopedResource[Membership] = ScopedResource("membership", Membership)


@members.rule(DataScope.SELF)
def _own_membership(actor: Actor) -> Q:
    return Q(pk=actor.membership.pk)


def _with_roles(qs: QuerySet[Membership]) -> QuerySet[Membership]:
    return qs.select_related("school", "user").prefetch_related(
        Prefetch(
            "role_assignments", queryset=MembershipRole.objects.select_related("role").order_by("role__key")
        )
    )


def member_list(actor: Actor) -> QuerySet[Membership]:
    return _with_roles(members.queryset(actor, "user.read")).order_by("id")


def member_detail(actor: Actor, membership_id: object, permission: str = "user.read") -> Membership:
    return members.get(actor, permission, membership_id, base=_with_roles(Membership.objects.all()))


def membership_with_roles(membership_id: uuid.UUID) -> Membership:
    """One membership with its school and roles, loaded now (call inside the right database context)."""
    return _with_roles(Membership.objects.filter(pk=membership_id)).get()


def memberships_for_user(user: User) -> QuerySet[Membership]:
    """The caller's own active memberships across schools (``/me``). RLS allows a user's own rows."""
    return _with_roles(Membership.objects.filter(user=user, is_active=True, school__is_active=True)).order_by(
        "school__name"
    )


def active_school_by_code(code: str) -> School | None:
    return School.objects.filter(code=code.strip().lower(), is_active=True).first()
