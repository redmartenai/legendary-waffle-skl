"""Invitations: the verified way a person's account joins a school and is linked to their profile (ADR-025).

An invitation is created by an authorised member of a school, delivered to one email address or mobile number,
and accepted by whoever proves control of that address with a one-time code. Until it is accepted it grants
nothing: no account, no membership, no role and no data scope.

Lifecycle (``InvitationStatus``)::

    pending ──accept──> accepted              (terminal)
       │  ──revoke──> revoked                 (terminal)
       └──expire──> expired ──resend──> pending
                       └──revoke──> revoked

Only a SHA-256 digest of the invitation secret is stored. The table is school-owned and protected by RLS
(invitations migration 0002); recipients reach it only by the secret's digest, under an explicit, logged
lookup (``services.find_by_token``).
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.academics.models import Campus, Department
from eduflow.authz.models import Role
from eduflow.core.ids import uuid7
from eduflow.identity.delivery import Channel
from eduflow.people.models import Guardian, StaffType, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class InvitationKind(models.TextChoices):
    STAFF = "staff", "Staff member or teacher"
    STUDENT = "student", "Student"
    GUARDIAN = "guardian", "Parent or guardian"


class InvitationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"


class Invitation(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    kind = models.CharField(max_length=16, choices=InvitationKind.choices)
    status = models.CharField(
        max_length=16, choices=InvitationStatus.choices, default=InvitationStatus.PENDING
    )
    channel = models.CharField(max_length=8, choices=Channel.choices)
    recipient = models.CharField(max_length=254, help_text="Normalised email address or E.164 mobile number.")
    full_name = models.CharField(max_length=200, help_text="Used only if acceptance creates a new account.")
    token_digest = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    roles = models.ManyToManyField(Role, through="InvitationRole", related_name="+")

    # What acceptance links the account to. Exactly one target per kind (see the check constraint).
    student = models.ForeignKey(Student, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    guardian = models.ForeignKey(Guardian, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    employee_id = models.CharField(max_length=32, blank=True)
    staff_type = models.CharField(max_length=16, choices=StaffType.choices, blank=True)
    designation = models.CharField(max_length=100, blank=True)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    campus = models.ForeignKey(Campus, on_delete=models.PROTECT, null=True, blank=True, related_name="+")

    expires_at = models.DateTimeField()
    last_sent_at = models.DateTimeField()
    send_count = models.PositiveSmallIntegerField(default=1)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_membership = models.ForeignKey(
        Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "invitations_invitation"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        kind=InvitationKind.STUDENT,
                        student__isnull=False,
                        guardian__isnull=True,
                        employee_id="",
                    )
                    | Q(
                        kind=InvitationKind.GUARDIAN,
                        guardian__isnull=False,
                        student__isnull=True,
                        employee_id="",
                    )
                    | (
                        Q(kind=InvitationKind.STAFF, student__isnull=True, guardian__isnull=True)
                        & ~Q(employee_id="")
                        & ~Q(staff_type="")
                    )
                ),
                name="invitations_invitation_target_matches_kind_check",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status=InvitationStatus.ACCEPTED,
                        accepted_at__isnull=False,
                        accepted_membership__isnull=False,
                    )
                    | (~Q(status=InvitationStatus.ACCEPTED) & Q(accepted_at__isnull=True))
                ),
                name="invitations_invitation_accepted_check",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=InvitationStatus.REVOKED, revoked_at__isnull=False)
                    | (~Q(status=InvitationStatus.REVOKED) & Q(revoked_at__isnull=True))
                ),
                name="invitations_invitation_revoked_check",
            ),
            # One open invitation per recipient and purpose; a student or guardian record has at most one.
            models.UniqueConstraint(
                fields=["school", "kind", "channel", "recipient"],
                condition=Q(status=InvitationStatus.PENDING),
                name="invitations_one_pending_per_recipient_uniq",
            ),
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(status=InvitationStatus.PENDING),
                name="invitations_one_pending_per_student_uniq",
            ),
            models.UniqueConstraint(
                fields=["guardian"],
                condition=Q(status=InvitationStatus.PENDING),
                name="invitations_one_pending_per_guardian_uniq",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="invitations_invitation_id_school_uniq"),
        ]
        indexes = [
            models.Index(fields=["school", "status"], name="invitations_school_status_idx"),
            # The expiry job scans open invitations by deadline.
            models.Index(
                fields=["expires_at"], condition=Q(status="pending"), name="invitations_pending_expiry_idx"
            ),
        ]

    def __str__(self) -> str:
        return str(self.id)


class InvitationRole(TenantModel):
    """A role the accepted member receives, assigned with the inviter's authority at acceptance time."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    invitation = models.ForeignKey(Invitation, on_delete=models.CASCADE, related_name="role_links")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "invitations_invitation_role"
        constraints = [
            models.UniqueConstraint(fields=["invitation", "role"], name="invitations_invitation_role_uniq"),
        ]

    def __str__(self) -> str:
        return str(self.id)
