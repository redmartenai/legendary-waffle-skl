"""Tenancy write operations: schools and memberships. Every change is audited."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.audit import services as audit
from eduflow.authz import services as authz_services
from eduflow.authz.catalog import ADMIN_ROLE
from eduflow.authz.models import Role
from eduflow.core.api import Conflict
from eduflow.identity.models import User
from eduflow.identity.phone import InvalidPhone, normalize_phone
from eduflow.identity.services import check_password_strength

from .models import Membership, School

if TYPE_CHECKING:
    from eduflow.authz.grants import Actor


@dataclass(frozen=True)
class PersonSpec:
    full_name: str
    email: str | None = None
    phone: str | None = None
    temporary_password: str | None = None


def _find_or_create_user(spec: PersonSpec) -> User:
    """Attach an existing account when the email or phone already belongs to one; never edit its profile."""
    email = (spec.email or "").strip().lower() or None
    try:
        phone = normalize_phone(spec.phone) if spec.phone else None
    except InvalidPhone as exc:
        raise ValidationError({"phone": [str(exc)]}) from None
    if not email and not phone:
        raise ValidationError({"non_field_errors": ["Give an email address or a mobile number."]})

    by_email = User.objects.filter(email=email).first() if email else None
    by_phone = User.objects.filter(phone=phone).first() if phone else None
    if by_email and by_phone and by_email.pk != by_phone.pk:
        raise Conflict("That email address and mobile number belong to different accounts.")
    existing = by_email or by_phone
    if existing is not None:
        return existing

    password = spec.temporary_password or None
    if password:
        check_password_strength(password, None, field="temporary_password")
    user = User.objects.create_user(
        email=email,
        phone=phone,
        password=password,
        full_name=spec.full_name,
        must_change_password=bool(password),
    )
    audit.record("identity.user.created", target_type="user", target_id=user.pk)
    return user


@transaction.atomic
def create_school(*, code: str, name: str, admin: PersonSpec | None, actor: User) -> School:
    """Platform onboarding: the school, its system roles and (optionally) its first school admin."""
    try:
        with transaction.atomic():
            school = School.objects.create(code=code, name=name)
    except IntegrityError:
        raise Conflict("A school with that code already exists.") from None
    roles = authz_services.seed_system_roles(school)
    audit.record(
        "tenancy.school.created",
        actor_id=actor.pk,
        school_id=school.pk,
        target_type="school",
        target_id=school.pk,
        metadata={"code": code},
    )
    if admin is not None:
        user = _find_or_create_user(admin)
        membership = _create_membership(school, user, actor_id=actor.pk)
        admin_role = next(r for r in roles if r.key == ADMIN_ROLE)
        authz_services.assign_role(None, membership, admin_role)
    return school


@transaction.atomic
def update_school(school: School, *, actor_id: object, **changes: object) -> School:
    fields = [f for f, v in changes.items() if v is not None]
    for field in fields:
        setattr(school, field, changes[field])
    if fields:
        school.save(update_fields=[*fields, "updated_at"])
        audit.record(
            "tenancy.school.updated",
            actor_id=actor_id,
            school_id=school.pk,
            target_type="school",
            target_id=school.pk,
            metadata={"fields": fields},
        )
    return school


def _create_membership(school: School, user: User, *, actor_id: object) -> Membership:
    try:
        with transaction.atomic():
            membership = Membership.objects.create(school=school, user=user)
    except IntegrityError:
        raise Conflict("That person is already a member of this school.") from None
    audit.record(
        "tenancy.membership.created",
        actor_id=actor_id,
        school_id=school.pk,
        target_type="membership",
        target_id=membership.pk,
        metadata={"user_id": str(user.pk)},
    )
    return membership


@transaction.atomic
def add_member(actor: Actor, person: PersonSpec, roles: list[Role]) -> Membership:
    user = _find_or_create_user(person)
    membership = _create_membership(actor.school, user, actor_id=actor.user.pk)
    for role in roles:
        authz_services.assign_role(actor, membership, role)
    return membership


@transaction.atomic
def set_membership_active(actor: Actor, membership: Membership, active: bool) -> Membership:
    if membership.is_active == active:
        return membership
    if membership.user_id == actor.user.pk:
        raise PermissionDenied("You cannot change your own membership.")
    if not active and authz_services.is_last_admin(membership):
        raise Conflict("A school must keep at least one active school admin.")
    membership.is_active = active
    membership.save(update_fields=["is_active", "updated_at"])
    authz_services.bump_rbac_version(membership.school_id)
    audit.record(
        "tenancy.membership.activated" if active else "tenancy.membership.deactivated",
        target_type="membership",
        target_id=membership.pk,
        metadata={"user_id": str(membership.user_id)},
    )
    return membership
