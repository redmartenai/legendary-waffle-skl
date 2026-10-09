"""Regression tests for the Phase 4 security review findings."""

from datetime import timedelta

import pytest
from django.utils import timezone

from eduflow.audit.models import AuditEvent
from eduflow.authz.models import MembershipRole, Role
from eduflow.authz.services import _set_grants, bump_rbac_version
from eduflow.core.config_validation import production_problems
from eduflow.core.tests.test_settings_prod import SECURE
from eduflow.identity.models import AuthSession, OtpChallenge, User
from eduflow.invitations.models import Invitation
from eduflow.tenancy.models import Membership

from .conftest import last_code, last_token

pytestmark = pytest.mark.django_db

PHONE = "+919811100001"


def _clerk(world, make_member, grants):
    clerk = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key=f"clerk-{clerk.pk.hex[:6]}", name="Clerk")
    _set_grants(role, grants)
    MembershipRole.objects.create(school=world.school, membership=clerk, role=role)
    bump_rbac_version(world.school.pk)
    return clerk


# ------------------------------------------------------------------------------------------------- finding 1
def test_unreachable_preregistered_account_is_claimed_by_the_verified_recipient(
    world, invite, accept, as_member
):
    # A school added this number earlier: no password, never verified, so nobody can sign in to it.
    created = as_member(world.admin).post(
        "/api/v1/memberships", {"full_name": "Pre", "phone": PHONE}, format="json"
    )
    assert created.status_code == 201
    owner = User.objects.get(phone=PHONE)
    response = accept(invite("student"))
    assert response.status_code == 200, response.content
    assert response.json()["session"] is not None
    owner.refresh_from_db()
    assert owner.phone_verified_at is not None
    assert AuditEvent.objects.filter(action="identity.account.claimed", target_id=str(owner.pk)).exists()


def test_reachable_accounts_are_never_claimed(world, invite, accept, make_user):
    make_user(phone=PHONE, password=None, email_verified_at=timezone.now(), email="has-email@example.test")
    assert accept(invite()).json()["error"]["code"] == "account_exists"


# ------------------------------------------------------------------------------------------------- finding 2
def test_resend_cannot_rearm_grants_beyond_the_resender(world, invite, as_member, make_member):
    admin_role = Role.objects.get(school=world.school, key="school_admin")
    sent = invite(role_ids=[str(admin_role.pk)])
    Invitation.objects.filter(pk=sent.id).update(
        expires_at=timezone.now() - timedelta(hours=1), last_sent_at=timezone.now() - timedelta(hours=80)
    )
    resender = _clerk(
        world,
        make_member,
        {
            "invitation.manage": ["school"],
            "invitation.read": ["school"],
            "user.create": ["school"],
            "user.update": ["school"],
            "staff.create": ["school"],
        },
    )
    assert as_member(resender).post(f"/api/v1/invitations/{sent.id}/resend").status_code == 403
    stale = Invitation.objects.get(pk=sent.id)
    assert stale.expires_at < timezone.now()  # not re-armed
    assert stale.send_count == 1


def test_resend_refuses_a_target_that_was_linked_meanwhile(world, invite, as_member, make_member):
    from eduflow.people.models import Student

    sent = invite("student")
    Invitation.objects.filter(pk=sent.id).update(last_sent_at=timezone.now() - timedelta(minutes=5))
    Student.objects.filter(pk=world.other_student.pk).update(membership_id=make_member(world.school).pk)
    assert as_member(world.admin).post(f"/api/v1/invitations/{sent.id}/resend").status_code == 409


# ------------------------------------------------------------------------------------------------- finding 3
def test_superseded_link_fails_even_if_looked_up_before_the_resend(world, invite, as_member, monkeypatch):
    from eduflow.invitations import services

    sent = invite()
    Invitation.objects.filter(pk=sent.id).update(last_sent_at=timezone.now() - timedelta(minutes=5))
    real_lookup = services.find_by_token

    def lookup_then_resend(raw):
        found = real_lookup(raw)
        as_member(world.admin).post(f"/api/v1/invitations/{sent.id}/resend")  # rotates the secret meanwhile
        return found

    monkeypatch.setattr(services, "find_by_token", lookup_then_resend)
    from eduflow.core.api import InvalidInvitation

    with pytest.raises(InvalidInvitation):
        services.request_verification(sent.token)
    assert last_token() != sent.token


# ------------------------------------------------------------------------------------------------- finding 5
def test_codes_for_two_invitations_to_one_address_do_not_interfere(
    world, other_world, invite, verify, as_member, api_client
):
    first = invite()
    other = as_member(other_world.admin).post(
        "/api/v1/invitations",
        {
            "kind": "student",
            "channel": "phone",
            "recipient": PHONE,
            "full_name": "X",
            "student_id": str(other_world.other_student.pk),
        },
        format="json",
    )
    assert other.status_code == 201
    second_token = last_token()
    first_challenge = verify(first)
    second = api_client.post("/api/v1/invitations/verification", {"token": second_token}, format="json")
    assert second.status_code == 200  # no cooldown across invitations
    assert OtpChallenge.objects.get(pk=first_challenge).invalidated_at is None


# ------------------------------------------------------------------------------------------------- finding 9
@pytest.mark.parametrize(
    "override",
    [
        {"EMAIL_BACKEND": "django.core.mail.backends.dummy.EmailBackend"},
        {"INVITATION_LINK_BASE": "https://app.example/invite?token="},
    ],
)
def test_production_refuses_silent_email_and_logged_links(override):
    assert production_problems({**SECURE, **override})


# ------------------------------------------------------------------------------------------------ finding 10
def test_revoke_needs_the_same_authority_as_create(world, invite, as_member, make_member):
    sent = invite()
    clerk = _clerk(world, make_member, {"invitation.manage": ["school"], "invitation.read": ["school"]})
    assert as_member(clerk).post(f"/api/v1/invitations/{sent.id}/revoke").status_code == 403
    assert Invitation.objects.get(pk=sent.id).status == "pending"


# -------------------------------------------------------------------------------------------- first password
def test_first_password_needs_a_fresh_sign_in(world, invite, accept, api_client):
    session = accept(invite()).json()["session"]
    AuthSession.objects.filter(user__phone=PHONE).update(created_at=timezone.now() - timedelta(hours=1))
    response = api_client.post(
        "/api/v1/auth/password/change",
        {"new_password": "My-own-first-password-1"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {session['access']}",
    )
    assert response.status_code == 403
    assert not User.objects.get(phone=PHONE).has_usable_password()
    assert Membership.objects.filter(user__phone=PHONE).exists()
    assert last_code()
