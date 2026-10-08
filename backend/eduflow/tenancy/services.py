"""Tenancy write operations: schools and memberships. Every change is audited."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone
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


# One message for every "cannot attach" case, so the response does not reveal which identifier matched
# what, or whether a matching account is verified.
_IDENTIFIER_IN_USE = (
    "That email address or mobile number is already in use. Ask the person to sign in to EduFlow once with "
    "it, then add them again."
)


def _find_or_create_user(spec: PersonSpec, *, trusted: bool) -> User:
    """Attach an existing account or create one. An existing account's profile is never edited.

    * An existing account is attached only if **every** identifier that matched it is verified. Otherwise a
      school could pre-register someone else's email or phone and receive the access other schools later
      grant to that person.
    * ``trusted`` (platform staff) creates accounts whose identifiers count as verified and may carry a
      temporary password. A school creates accounts with no password and unverified identifiers: the person
      signs in by phone OTP, which verifies the phone.
    """
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
        raise Conflict(_IDENTIFIER_IN_USE)
    existing = by_email or by_phone
    if existing is not None:
        unverified = (by_email is not None and existing.email_verified_at is None) or (
            by_phone is not None and existing.phone_verified_at is None
        )
        if unverified:
            raise Conflict(_IDENTIFIER_IN_USE)
        return existing

    password = (spec.temporary_password or None) if trusted else None
    if password:
        check_password_strength(password, None, field="temporary_password")
    verified_at = timezone.now() if trusted else None
    user = User.objects.create_user(
        email=email,
        phone=phone,
        password=password,
        full_name=spec.full_name,
        must_change_password=bool(password),
        email_verified_at=verified_at if email else None,
        phone_verified_at=verified_at if phone else None,
    )
    audit.record(
        "identity.user.created", target_type="user", target_id=user.pk, metadata={"by_platform": trusted}
    )
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
        user = _find_or_create_user(admin, trusted=True)
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
    user = _find_or_create_user(person, trusted=False)
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
    # Re-activating restores the member's roles, and deactivating removes them: both need the right to
    # grant everything those roles carry.
    authz_services.ensure_can_manage_roles_of(actor, membership)
    authz_services.lock_school(membership.school_id)
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
