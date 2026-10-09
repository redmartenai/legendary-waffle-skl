"""Invitation lifecycle: create, resend, revoke, expire, and the recipient's verify-and-accept (ADR-025).

School side (an authorised member, through ``TenantAPIView``):

* **create**: needs, school-wide, ``user.create`` + ``user.update`` (it will create a membership and link an
  account) plus the target's write permission (``staff.create`` / ``student.update`` /
  ``guardian.manage``), and
  the right to give every role the invitation carries (Phase 2 escalation rule). Nothing is saved unless the
  message was handed to a provider.
* **resend**: a new secret (the old link dies) and a new deadline, at most once per
  ``INVITATION_RESEND_SECONDS``.
* **revoke**: pending or expired invitations only. Never touches accounts or memberships.

Recipient side (no school context; the secret is the only key):

* **preview** and **verification** find the invitation by the secret's digest, then work inside the
  invitation's school context.
* **accept** needs a one-time code sent to the invitation's own address, proving control of exactly that email
  or number. Everything happens in one transaction: the account (new, or the signed-in one), the membership,
  the roles, the profile link and the invitation's state. The roles and the link are applied with the
  *inviter's* current authority, so an inviter who has lost it (or left) cannot be impersonated by an old
  link.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError

from eduflow.academics.models import Campus, Department, RecordStatus
from eduflow.audit import services as audit
from eduflow.audit.models import Outcome
from eduflow.authz import services as authz_services
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor, compute_grants
from eduflow.authz.models import Role
from eduflow.branding import selectors as branding_selectors
from eduflow.core import db_context
from eduflow.core.api import AccountExists, Conflict, InvalidInvitation, InvalidOtp, ServiceUnavailable
from eduflow.core.request_context import bind_request_info
from eduflow.identity import services as identity_services
from eduflow.identity.delivery import Channel, DeliveryUnavailable, channel_enabled, deliver, mask_address
from eduflow.identity.models import User
from eduflow.identity.otp import service as otp
from eduflow.identity.phone import InvalidPhone, normalize_phone
from eduflow.identity.tokens import IssuedTokens
from eduflow.people import services as people_services
from eduflow.people.models import Guardian, StaffProfile, StaffType, Student, StudentStatus
from eduflow.tenancy import domain
from eduflow.tenancy import selectors as tenancy_selectors
from eduflow.tenancy import services as tenancy_services
from eduflow.tenancy.models import Membership

from .models import Invitation, InvitationKind, InvitationRole, InvitationStatus

# The write permission each kind needs (school-wide), besides user.create and user.update.
_TARGET_PERMISSION = {
    InvitationKind.STAFF: "staff.create",
    InvitationKind.STUDENT: "student.update",
    InvitationKind.GUARDIAN: "guardian.manage",
}
# Students and guardians always get their system role; staff roles are chosen by the inviter.
_FIXED_ROLE = {InvitationKind.STUDENT: "student", InvitationKind.GUARDIAN: "parent"}


@dataclass(frozen=True)
class NewInvitation:
    kind: str
    channel: str
    recipient: str
    full_name: str
    role_ids: list[uuid.UUID] = field(default_factory=list)
    student_id: uuid.UUID | None = None
    guardian_id: uuid.UUID | None = None
    employee_id: str = ""
    staff_type: str = ""
    designation: str = ""
    department_id: uuid.UUID | None = None
    campus_id: uuid.UUID | None = None


@dataclass(frozen=True)
class InvitationPreview:
    school_name: str
    branding: dict[str, Any]
    kind: str
    role_names: list[str]
    channel: str
    recipient_hint: str
    expires_at: datetime


@dataclass(frozen=True)
class Acceptance:
    membership: Membership
    session: IssuedTokens | None  # set when acceptance created the account


# ------------------------------------------------------------------------------------------------ helpers
def token_digest(raw: str) -> str:
    """SHA-256 of the secret. The secret has 256 bits of entropy, so an unsalted digest cannot be reversed."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_secret() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, token_digest(raw)


def normalize_recipient(channel: str, value: str) -> str:
    if channel == Channel.PHONE:
        try:
            return normalize_phone(value)
        except InvalidPhone as exc:
            raise ValidationError({"recipient": [str(exc)]}) from None
    email = value.strip().lower()
    try:
        validate_email(email)
    except DjangoValidationError:
        raise ValidationError({"recipient": ["Enter a valid email address."]}) from None
    return email


def _has_school_scope(actor: Actor, *permissions: str) -> bool:
    return all(DataScope.SCHOOL in actor.scopes(p) for p in permissions)


def _required_permissions(kind: str) -> tuple[str, ...]:
    return ("invitation.manage", "user.create", "user.update", _TARGET_PERMISSION[InvitationKind(kind)])


def _expires_at(now: datetime) -> datetime:
    return now + timedelta(hours=int(settings.INVITATION_TTL_HOURS))


def _send_link(invitation: Invitation, raw: str) -> None:
    link = f"{settings.INVITATION_LINK_BASE}{raw}"
    body = (
        f"{invitation.school.name} has invited you to EduFlow. Open this link to accept: {link} "
        f"(valid until {invitation.expires_at:%Y-%m-%d %H:%M} UTC)."
    )
    try:
        deliver(
            invitation.channel,
            invitation.recipient,
            subject=f"Your invitation to {invitation.school.name}",
            body=body,
        )
    except DeliveryUnavailable:
        # Raised inside the caller's transaction: nothing is saved for an invitation nobody received.
        raise ServiceUnavailable() from None


def _expire_if_due(invitation: Invitation, now: datetime) -> None:
    if invitation.status == InvitationStatus.PENDING and invitation.expires_at <= now:
        invitation.status = InvitationStatus.EXPIRED
        invitation.save(update_fields=["status", "updated_at"])
        domain.record("invitations.invitation.expired", invitation)


def _locked(invitation_id: uuid.UUID, *, raw: str | None = None) -> Invitation:
    """Lock the invitation row. With ``raw``, the secret must still be current *under the lock*, so a link
    superseded by a concurrent resend cannot slip through between lookup and lock."""
    rows = (
        Invitation.objects.select_for_update(of=("self",)).select_related("school").filter(pk=invitation_id)
    )
    if raw is not None:
        rows = rows.filter(token_digest=token_digest(raw))
    invitation = rows.first()
    if invitation is None:
        raise InvalidInvitation()
    return invitation


# ------------------------------------------------------------------------------------------------ school side
def _roles_for(actor: Actor, request: NewInvitation) -> list[Role]:
    kind = InvitationKind(request.kind)
    school_roles = Role.objects.for_school(actor.school)
    if kind in _FIXED_ROLE:
        if request.role_ids:
            raise ValidationError(
                {"role_ids": ["Student and guardian invitations always carry their own role."]}
            )
        return [school_roles.get(key=_FIXED_ROLE[kind])]
    roles = list(school_roles.filter(pk__in=request.role_ids))
    if not request.role_ids or len(roles) != len(set(request.role_ids)):
        raise ValidationError({"role_ids": ["Choose one or more roles of this school."]})
    return roles


def _target_fields(actor: Actor, request: NewInvitation) -> dict[str, object]:
    kind = InvitationKind(request.kind)
    if kind == InvitationKind.STUDENT:
        student = domain.resolve(Student, actor.school, request.student_id, "student_id", label="student")
        if student.membership_id is not None:
            raise ValidationError({"student_id": ["This student is already linked to an account."]})
        if student.status != StudentStatus.ACTIVE:
            raise ValidationError({"student_id": ["This student is not active."]})
        return {"student": student}
    if kind == InvitationKind.GUARDIAN:
        guardian = domain.resolve(
            Guardian, actor.school, request.guardian_id, "guardian_id", label="guardian"
        )
        if guardian.membership_id is not None:
            raise ValidationError({"guardian_id": ["This guardian is already linked to an account."]})
        return {"guardian": guardian}

    if not request.employee_id:
        raise ValidationError({"employee_id": ["Required for a staff invitation."]})
    if StaffProfile.objects.for_school(actor.school).filter(employee_id=request.employee_id).exists():
        raise Conflict("That employee ID is already in use in this school.")
    return {
        "employee_id": request.employee_id,
        "staff_type": request.staff_type or StaffType.TEACHING,
        "designation": request.designation,
        "department": _active_reference(Department, actor, request.department_id, "department_id"),
        "campus": _active_reference(Campus, actor, request.campus_id, "campus_id"),
    }


def _active_reference[R: (Department, Campus)](
    model: type[R], actor: Actor, pk: uuid.UUID | None, field_name: str
) -> R | None:
    if pk is None:
        return None
    ref = domain.resolve(model, actor.school, pk, field_name)
    if ref.status != RecordStatus.ACTIVE:
        raise ValidationError({field_name: ["This record is archived."]})
    return ref


def _is_own_address(user: User, channel: str, recipient: str) -> bool:
    return recipient == (user.phone if channel == Channel.PHONE else user.email)


@transaction.atomic
def create_invitation(actor: Actor, request: NewInvitation) -> Invitation:
    if not _has_school_scope(actor, *_required_permissions(request.kind)):
        raise PermissionDenied(
            "Inviting this kind of member needs school-wide member and record administration."
        )
    if request.kind != InvitationKind.STAFF and any(
        (
            request.employee_id,
            request.staff_type,
            request.designation,
            request.department_id,
            request.campus_id,
        )
    ):
        raise ValidationError({"non_field_errors": ["Staff details belong only to staff invitations."]})
    if not channel_enabled(request.channel):
        raise ServiceUnavailable()
    recipient = normalize_recipient(request.channel, request.recipient)
    if _is_own_address(actor.user, request.channel, recipient):
        raise ValidationError({"recipient": ["You cannot invite yourself."]})
    roles = _roles_for(actor, request)
    for role in roles:
        authz_services.ensure_can_assign(actor, role)

    now = timezone.now()
    raw, digest = _new_secret()
    invitation = domain.save(
        Invitation(
            school=actor.school,
            kind=request.kind,
            channel=request.channel,
            recipient=recipient,
            full_name=request.full_name,
            token_digest=digest,
            invited_by=actor.membership,
            expires_at=_expires_at(now),
            last_sent_at=now,
            **_target_fields(actor, request),
        ),
        conflict="An open invitation already exists for this person or record. Resend or revoke it instead.",
    )
    InvitationRole.objects.bulk_create(
        InvitationRole(school=actor.school, invitation=invitation, role=role) for role in roles
    )
    _send_link(invitation, raw)
    domain.record(
        "invitations.invitation.created",
        invitation,
        kind=invitation.kind,
        channel=invitation.channel,
        roles=sorted(r.key for r in roles),
    )
    return invitation


def _ensure_target_still_open(invitation: Invitation) -> None:
    """The record an invitation links to must still be linkable when the invitation is re-armed."""
    if invitation.student is not None and (
        invitation.student.membership_id is not None or invitation.student.status != StudentStatus.ACTIVE
    ):
        raise Conflict("This student is already linked to an account or no longer active.")
    if invitation.guardian is not None and invitation.guardian.membership_id is not None:
        raise Conflict("This guardian is already linked to an account.")
    if (
        invitation.kind == InvitationKind.STAFF
        and StaffProfile.objects.for_school(invitation.school_id)
        .filter(employee_id=invitation.employee_id)
        .exists()
    ):
        raise Conflict("That employee ID is already in use in this school.")


@transaction.atomic
def resend_invitation(actor: Actor, invitation: Invitation) -> Invitation:
    """Issue a fresh secret and deadline. The previous link stops working."""
    now = timezone.now()
    invitation = _locked(invitation.pk)
    _expire_if_due(invitation, now)
    if invitation.status not in (InvitationStatus.PENDING, InvitationStatus.EXPIRED):
        raise Conflict(f"This invitation is {invitation.status}; it cannot be resent.")
    if not _has_school_scope(actor, *_required_permissions(invitation.kind)):
        raise PermissionDenied()
    # Resending re-arms what the invitation grants, so the resender must be able to grant it themselves.
    for role in invitation.roles.all():
        authz_services.ensure_can_assign(actor, role)
    _ensure_target_still_open(invitation)
    cooldown = timedelta(seconds=int(settings.INVITATION_RESEND_SECONDS))
    if invitation.last_sent_at > now - cooldown:
        wait = (invitation.last_sent_at + cooldown - now).total_seconds()
        raise Throttled(wait=max(1, math.ceil(wait)))
    if not channel_enabled(invitation.channel):
        raise ServiceUnavailable()

    raw, digest = _new_secret()
    invitation.token_digest = digest
    invitation.status = InvitationStatus.PENDING
    invitation.expires_at = _expires_at(now)
    invitation.last_sent_at = now
    invitation.send_count += 1
    domain.save(
        invitation,
        conflict="Another open invitation exists for this person or record.",
        update_fields=["token_digest", "status", "expires_at", "last_sent_at", "send_count"],
    )
    _send_link(invitation, raw)
    domain.record("invitations.invitation.resent", invitation, send_count=invitation.send_count)
    return invitation


@transaction.atomic
def revoke_invitation(actor: Actor, invitation: Invitation) -> Invitation:
    """Close an open invitation. Accounts and memberships, even ones it once created, are never touched."""
    now = timezone.now()
    invitation = _locked(invitation.pk)
    _expire_if_due(invitation, now)
    if invitation.status not in (InvitationStatus.PENDING, InvitationStatus.EXPIRED):
        raise Conflict(f"This invitation is {invitation.status}; it cannot be revoked.")
    if not _has_school_scope(actor, *_required_permissions(invitation.kind)):
        raise PermissionDenied()
    invitation.status = InvitationStatus.REVOKED
    invitation.revoked_at = now
    invitation.revoked_by = actor.membership
    invitation.save(update_fields=["status", "revoked_at", "revoked_by", "updated_at"])
    domain.record("invitations.invitation.revoked", invitation)
    return invitation


def expire_due_invitations(limit: int = 500) -> int:
    """Platform maintenance (beat): mark overdue pending invitations ``expired`` in every school."""
    now = timezone.now()
    expired = 0
    with db_context.system_context("invitation_expiry"), transaction.atomic():
        due = (
            Invitation.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status=InvitationStatus.PENDING, expires_at__lte=now)
            .order_by("expires_at")[:limit]
        )
        for invitation in due:
            invitation.status = InvitationStatus.EXPIRED
            invitation.save(update_fields=["status", "updated_at"])
            audit.record(
                "invitations.invitation.expired",
                actor_id=None,
                school_id=invitation.school_id,
                target_type="invitation",
                target_id=invitation.pk,
            )
            expired += 1
    return expired


# ------------------------------------------------------------------------------------------- recipient side
def find_by_token(raw: str) -> tuple[uuid.UUID, uuid.UUID]:
    """``(invitation_id, school_id)`` for a secret, or ``InvalidInvitation``.

    The recipient has no school context yet, so this one indexed lookup by digest runs under the explicit,
    logged RLS bypass. It returns identifiers only; all further work happens in the invitation's school
    context.
    """
    with db_context.system_context("invitation_lookup"):
        row = Invitation.objects.filter(token_digest=token_digest(raw)).values_list("id", "school_id").first()
    if row is None:
        raise InvalidInvitation()
    return row


@contextmanager
def _in_school(school_id: uuid.UUID, user: User | None = None) -> Iterator[None]:
    bind_request_info(school_id=str(school_id))
    with db_context.scoped(db_context.DbContext(school_id=school_id, user_id=user.pk if user else None)):
        yield


def _usable(invitation: Invitation, now: datetime) -> bool:
    return (
        invitation.status == InvitationStatus.PENDING
        and invitation.expires_at > now
        and invitation.school.is_active
    )


def preview(raw: str) -> InvitationPreview:
    invitation_id, school_id = find_by_token(raw)
    with _in_school(school_id):
        invitation = (
            Invitation.objects.select_related("school").prefetch_related("roles").get(pk=invitation_id)
        )
        if not _usable(invitation, timezone.now()):
            raise InvalidInvitation()
        return InvitationPreview(
            school_name=invitation.school.name,
            branding=branding_selectors.public_branding(invitation.school),
            kind=invitation.kind,
            role_names=sorted(role.name for role in invitation.roles.all()),
            channel=invitation.channel,
            recipient_hint=mask_address(invitation.channel, invitation.recipient),
            expires_at=invitation.expires_at,
        )


def request_verification(raw: str) -> otp.OtpRequestResult:
    """Send a one-time code to the invitation's own address (never to an address the caller chooses)."""
    invitation_id, school_id = find_by_token(raw)
    with _in_school(school_id), transaction.atomic():
        invitation = _locked(invitation_id, raw=raw)
        if not _usable(invitation, timezone.now()):
            raise InvalidInvitation()
        result = otp.request_invitation_code(
            invitation_id=invitation.pk, channel=invitation.channel, address=invitation.recipient
        )
        domain.record("invitations.verification.requested", invitation, channel=invitation.channel)
    return result


def _inviter(invitation: Invitation) -> Actor:
    """The inviter as they are *now*: an inviter who lost the authority, or left, voids the invitation."""
    membership = Membership.objects.select_related("user", "school").get(pk=invitation.invited_by_id)
    if not (membership.is_active and membership.user.is_active):
        raise InvalidInvitation()
    actor = Actor(
        user=membership.user,
        school=membership.school,
        membership=membership,
        grants=compute_grants(membership),
    )
    if not _has_school_scope(actor, *_required_permissions(invitation.kind)):
        raise InvalidInvitation()
    return actor


def _claimable(owner: User, verified_field: str) -> bool:
    """An account nobody can sign in to: created for this address by a school, never verified, no password.

    Whoever proves control of the address is its rightful owner (exactly as a first phone OTP sign-in would
    make them), so they may take it over instead of being told to sign in to an account they cannot reach.
    """
    return (
        owner.is_active
        and getattr(owner, verified_field) is None
        and owner.email_verified_at is None
        and owner.phone_verified_at is None
        and not owner.has_usable_password()
    )


def _account(invitation: Invitation, signed_in: User | None) -> tuple[User, bool]:
    """The account that accepts. Returns (user, sign_in): ``sign_in`` when the person gets a new session
    (a new account, or a claimed one), as opposed to accepting with the account they are signed in to."""
    identifier = "phone" if invitation.channel == Channel.PHONE else "email"
    verified_field = f"{identifier}_verified_at"
    owner = User.objects.filter(**{identifier: invitation.recipient}).first()
    now = timezone.now()

    if signed_in is None:
        if owner is not None and _claimable(owner, verified_field):
            setattr(owner, verified_field, now)
            owner.save(update_fields=[verified_field, "updated_at"])
            audit.record(
                "identity.account.claimed",
                actor_id=owner.pk,
                target_type="user",
                target_id=owner.pk,
                metadata={"via": "invitation", "channel": invitation.channel},
            )
            return owner, True
        if owner is not None:
            raise AccountExists()
        try:
            with transaction.atomic():
                if invitation.channel == Channel.PHONE:
                    user = User.objects.create_user(
                        phone=invitation.recipient, full_name=invitation.full_name, phone_verified_at=now
                    )
                else:
                    user = User.objects.create_user(
                        email=invitation.recipient, full_name=invitation.full_name, email_verified_at=now
                    )
        except (IntegrityError, DjangoValidationError):  # created concurrently by another request
            raise AccountExists() from None
        audit.record(
            "identity.user.created",
            actor_id=user.pk,
            target_type="user",
            target_id=user.pk,
            metadata={"via": "invitation"},
        )
        return user, True

    if owner is not None and owner.pk != signed_in.pk:
        raise Conflict(
            "This email address or mobile number belongs to a different account. "
            "Sign in to that account to accept."
        )
    if getattr(signed_in, identifier) is None:
        # The code proved control of the address, so it becomes this account's verified identifier.
        setattr(signed_in, identifier, invitation.recipient)
        setattr(signed_in, verified_field, now)
        domain.save(
            signed_in, conflict="That address is already in use.", update_fields=[identifier, verified_field]
        )
    elif (
        getattr(signed_in, identifier) == invitation.recipient and getattr(signed_in, verified_field) is None
    ):
        setattr(signed_in, verified_field, now)
        signed_in.save(update_fields=[verified_field, "updated_at"])
    return signed_in, False


def _membership(invitation: Invitation, account: User, inviter: Actor) -> Membership:
    existing = Membership.objects.for_school(invitation.school_id).filter(user=account).first()
    if existing is None:
        return tenancy_services.create_membership(invitation.school, account, actor_id=inviter.user.pk)
    if not existing.is_active:
        # An invitation never reactivates a membership the school deactivated.
        raise Conflict("Your membership of this school is deactivated. Contact the school.")
    return existing


def _link_profile(invitation: Invitation, inviter: Actor, membership: Membership) -> None:
    if invitation.kind == InvitationKind.STUDENT and invitation.student is not None:
        people_services.link_student_account(inviter, invitation.student, membership)
    elif invitation.kind == InvitationKind.GUARDIAN and invitation.guardian is not None:
        people_services.link_guardian_account(inviter, invitation.guardian, membership)
    elif invitation.kind == InvitationKind.STAFF:
        people_services.create_staff(
            inviter,
            membership_id=membership.pk,
            employee_id=invitation.employee_id,
            staff_type=invitation.staff_type,
            designation=invitation.designation,
            department_id=invitation.department_id,
            campus_id=invitation.campus_id,
        )


def _complete(invitation: Invitation, signed_in: User | None) -> tuple[Membership, User | None]:
    inviter = _inviter(invitation)
    account, signs_in = _account(invitation, signed_in)
    if account.pk == inviter.user.pk:
        raise ValidationError(
            {"non_field_errors": ["An invitation cannot be accepted by the person who sent it."]}
        )
    membership = _membership(invitation, account, inviter)
    held = set(membership.role_assignments.values_list("role_id", flat=True))
    try:
        for role in invitation.roles.all():
            if role.pk not in held:
                authz_services.assign_role(inviter, membership, role)
        _link_profile(invitation, inviter, membership)
    except PermissionDenied:  # the inviter's authority no longer covers what the invitation grants
        raise InvalidInvitation() from None

    invitation.status = InvitationStatus.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.accepted_membership = membership
    invitation.save(update_fields=["status", "accepted_at", "accepted_membership", "updated_at"])
    audit.record(
        "invitations.invitation.accepted",
        actor_id=account.pk,
        school_id=invitation.school_id,
        target_type="invitation",
        target_id=invitation.pk,
        metadata={
            "kind": invitation.kind,
            "signed_in_by_acceptance": signs_in,
            "membership": str(membership.pk),
        },
    )
    return membership, account if signs_in else None


def accept_invitation(raw: str, *, challenge_id: str, code: str, signed_in: User | None) -> Acceptance:
    """Verify the code and accept, all-or-nothing. A wrong code is counted even though nothing is accepted."""
    invitation_id, school_id = find_by_token(raw)
    new_account: User | None = None
    with _in_school(school_id, signed_in):
        with transaction.atomic():
            invitation = _locked(invitation_id, raw=raw)
            if not _usable(invitation, timezone.now()):
                raise InvalidInvitation()
            verified = otp.consume_invitation_code(
                challenge_id=challenge_id, code=code, invitation_id=invitation.pk
            )
            if not verified:
                audit.record(
                    "invitations.invitation.verification_failed",
                    outcome=Outcome.FAILURE,
                    actor_id=signed_in.pk if signed_in else None,
                    school_id=school_id,
                    target_type="invitation",
                    target_id=invitation.pk,
                )
            else:
                membership, new_account = _complete(invitation, signed_in)
        # Outside the atomic block, so the counted attempt and the audit record are committed.
        if not verified:
            raise InvalidOtp()
        # Loaded inside the school context: a brand-new account has no user context of its own yet.
        membership = tenancy_selectors.membership_with_roles(membership.pk)
    session = identity_services.invitation_login(new_account) if new_account is not None else None
    return Acceptance(membership=membership, session=session)
