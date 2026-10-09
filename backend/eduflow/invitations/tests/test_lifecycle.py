"""Invitation lifecycle end to end: create, preview, verify, accept (new and existing accounts), resend,
revoke, expire. Each test checks a security invariant, not just a status code."""

from datetime import timedelta

import pytest
import time_machine
from django.core import mail
from django.utils import timezone

from conftest import PASSWORD
from eduflow.audit.models import AuditEvent
from eduflow.identity.models import User
from eduflow.identity.otp.providers import MemorySmsProvider
from eduflow.invitations.models import Invitation
from eduflow.invitations.services import expire_due_invitations, token_digest
from eduflow.people.models import StaffProfile
from eduflow.tenancy.models import Membership

from .conftest import last_code, last_token

pytestmark = pytest.mark.django_db

PHONE = "+919811100001"


# ---------------------------------------------------------------------------------------------------- create
def test_create_stores_only_a_digest_and_never_returns_the_secret(world, invite):
    sent = invite()
    row = Invitation.objects.get(pk=sent.id)
    assert row.token_digest == token_digest(sent.token)
    assert sent.token not in row.token_digest
    body = str(sent.body)
    assert sent.token not in body
    assert row.token_digest not in body
    assert PHONE not in body
    assert sent.body["recipient_hint"] == "+91********01"
    assert sent.body["status"] == "pending"
    assert [r["key"] for r in sent.body["roles"]] == ["teacher"]
    event = AuditEvent.objects.get(action="invitations.invitation.created")
    assert sent.token not in str(event.metadata)
    assert PHONE not in str(event.metadata)


def test_a_pending_invitation_grants_nothing(world, invite):
    invite()
    assert not User.objects.filter(phone=PHONE).exists()
    assert Membership.objects.filter(school=world.school).count() == 5  # the fixture's members only


def test_email_invitation_goes_by_email(world, invite):
    sent = invite(channel="email", recipient="New.Teacher@Example.test")
    assert mail.outbox[-1].to == ["new.teacher@example.test"]
    assert sent.body["recipient_hint"] == "ne***@example.test"


# --------------------------------------------------------------------------------------------------- preview
def test_preview_shows_safe_details_only(world, invite, api_client):
    sent = invite()
    response = api_client.post("/api/v1/invitations/preview", {"token": sent.token}, format="json")
    assert response.status_code == 200
    assert response.json() == {
        "school_name": world.school.name,
        "kind": "staff",
        "role_names": ["Teacher"],
        "channel": "phone",
        "recipient_hint": "+91********01",
        "expires_at": response.json()["expires_at"],
    }


@pytest.mark.parametrize("state", ["unknown", "revoked", "expired", "accepted"])
def test_unusable_secrets_are_indistinguishable(world, invite, accept, api_client, as_member, state):
    sent = invite()
    token = sent.token
    if state == "unknown":
        token = "x" * 43
    elif state == "revoked":
        as_member(world.admin).post(f"/api/v1/invitations/{sent.id}/revoke")
    elif state == "expired":
        Invitation.objects.filter(pk=sent.id).update(expires_at=timezone.now() - timedelta(seconds=1))
    else:
        assert accept(sent).status_code == 200
    response = api_client.post("/api/v1/invitations/preview", {"token": token}, format="json")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invitation_invalid"
    assert (
        api_client.post("/api/v1/invitations/verification", {"token": token}, format="json").status_code
        == 404
    )


# --------------------------------------------------------------------------------------- accept: new account
def test_new_account_acceptance_creates_exactly_the_invited_access(world, invite, accept, api_client):
    sent = invite(designation="Maths teacher")
    response = accept(sent)
    assert response.status_code == 200, response.content
    body = response.json()

    user = User.objects.get(phone=PHONE)
    assert user.phone_verified_at is not None
    assert user.email is None
    assert not user.has_usable_password()  # first password via /auth/password/change
    membership = Membership.objects.get(school=world.school, user=user)
    assert body["membership"]["id"] == str(membership.pk)
    assert [r["key"] for r in body["membership"]["roles"]] == ["teacher"]
    staff = StaffProfile.objects.get(membership=membership)
    assert (staff.employee_id, staff.designation) == ("T-NEW", "Maths teacher")

    # The new user is signed in and can act in the school.
    session = body["session"]
    me = api_client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {session['access']}")
    assert me.status_code == 200
    invitation = Invitation.objects.get(pk=sent.id)
    assert invitation.status == "accepted"
    assert invitation.accepted_membership_id == membership.pk
    event = AuditEvent.objects.get(action="invitations.invitation.accepted")
    assert event.actor_id == user.pk
    assert event.metadata["signed_in_by_acceptance"] is True


def test_new_account_by_email_verifies_only_the_email(world, invite, accept):
    sent = invite(channel="email", recipient="fresh@example.test")
    assert accept(sent).status_code == 200
    user = User.objects.get(email="fresh@example.test")
    assert user.email_verified_at is not None
    assert user.phone is None
    assert user.phone_verified_at is None


def test_first_password_after_acceptance(world, invite, accept, api_client):
    session = accept(invite()).json()["session"]
    response = api_client.post(
        "/api/v1/auth/password/change",
        {"new_password": "My-own-first-password-1"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {session['access']}",
    )
    assert response.status_code == 204


def test_anonymous_acceptance_never_takes_over_an_existing_account(world, invite, accept, make_user):
    owner = make_user(phone=PHONE)
    response = accept(invite())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "account_exists"
    assert not Membership.objects.filter(school=world.school, user=owner).exists()
    assert Invitation.objects.get(recipient=PHONE).status == "pending"


# ---------------------------------------------------------------------------------- accept: existing account
def test_signed_in_acceptance_keeps_credentials_and_other_memberships(
    world, other_world, invite, accept, make_user, make_member, client_for, api_client
):
    user = make_user(phone=PHONE, phone_verified_at=timezone.now())
    elsewhere = make_member(other_world.school, user, roles=["parent"])
    response = accept(invite(), client=client_for(user))
    assert response.status_code == 200, response.content
    assert response.json()["session"] is None
    assert Membership.objects.get(school=world.school, user=user).is_active
    elsewhere.refresh_from_db()
    assert elsewhere.is_active
    login = api_client.post(
        "/api/v1/auth/password/login", {"identifier": PHONE, "password": PASSWORD}, format="json"
    )
    assert login.status_code == 200
    assert {m["school"]["id"] for m in login.json()["memberships"]} == {
        str(world.school.pk),
        str(other_world.school.pk),
    }


def test_signed_in_account_gains_the_proven_address(world, invite, accept, make_user, client_for):
    user = make_user(email="someone@example.test")
    assert accept(invite(), client=client_for(user)).status_code == 200
    user.refresh_from_db()
    assert user.phone == PHONE
    assert user.phone_verified_at is not None


def test_a_different_account_cannot_accept_for_an_address_it_does_not_own(
    world, invite, accept, make_user, client_for
):
    make_user(phone=PHONE)
    intruder = make_user(email="intruder@example.test")
    response = accept(invite(), client=client_for(intruder))
    assert response.status_code == 409
    assert not Membership.objects.filter(school=world.school, user=intruder).exists()


def test_existing_member_gains_roles_and_profile_on_the_same_membership(world, invite, accept, client_for):
    from eduflow.people.models import Guardian, StudentGuardian

    teacher = world.teacher.user
    teacher.phone = PHONE
    teacher.phone_verified_at = timezone.now()
    teacher.save()
    guardian = Guardian.objects.create(school=world.school, full_name="Teacher as parent")
    StudentGuardian.objects.create(
        school=world.school, student=world.other_student, guardian=guardian, relationship="father"
    )
    response = accept(invite("guardian", guardian_id=guardian.pk), client=client_for(teacher))
    assert response.status_code == 200, response.content
    assert {r["key"] for r in response.json()["membership"]["roles"]} == {"teacher", "parent"}
    guardian.refresh_from_db()
    assert guardian.membership_id == world.teacher.pk


def test_deactivated_membership_is_not_reactivated(world, invite, accept, make_member, make_user, client_for):
    user = make_user(phone=PHONE, phone_verified_at=timezone.now())
    member = make_member(world.school, user, roles=[], is_active=False)
    response = accept(invite(), client=client_for(user))
    assert response.status_code == 409
    member.refresh_from_db()
    assert member.is_active is False
    assert Invitation.objects.get().status == "pending"


# ----------------------------------------------------------------------------------------------------- codes
def test_wrong_code_is_counted_and_accepts_nothing(world, invite, accept, verify):
    sent = invite()
    challenge = verify(sent)
    good = last_code()
    for _ in range(5):
        wrong = f"{(int(good) + 1) % 1_000_000:06d}"
        response = accept(sent, code=wrong, challenge_id=challenge)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_code"
    # Locked: even the right code fails now; a fresh code is needed.
    assert accept(sent, code=good, challenge_id=challenge).status_code == 401
    assert not User.objects.filter(phone=PHONE).exists()
    assert AuditEvent.objects.filter(action="invitations.invitation.verification_failed").count() == 6


def test_code_is_single_use_and_bound_to_its_invitation(world, invite, accept, verify, api_client):
    first = invite()
    second = invite(recipient="+919811100002", employee_id="T-OTHER")
    challenge = verify(first)
    code = last_code()
    # The first invitation's code does not accept the second invitation.
    assert accept(second, code=code, challenge_id=challenge).status_code == 401
    assert accept(first, code=code, challenge_id=challenge).status_code == 200
    # Replaying the accepted invitation (or its code) fails.
    assert accept(first, code=code, challenge_id=challenge).status_code == 404


def test_sign_in_code_cannot_accept_an_invitation(world, invite, accept, make_user, api_client):
    make_user(phone="+919811100009", password=None)
    sent = invite()
    login = api_client.post("/api/v1/auth/otp/request", {"phone": "+919811100009"}, format="json").json()
    response = accept(sent, code=last_code(), challenge_id=login["challenge_id"])
    assert response.status_code == 401


def test_verification_cooldown(world, invite, verify, api_client):
    sent = invite()
    verify(sent)
    again = api_client.post("/api/v1/invitations/verification", {"token": sent.token}, format="json")
    assert again.status_code == 429


# ---------------------------------------------------------------------------------- resend / revoke / expire
def test_resend_rotates_the_secret_and_respects_the_cooldown(world, invite, as_member, api_client):
    sent = invite()
    admin = as_member(world.admin)
    assert admin.post(f"/api/v1/invitations/{sent.id}/resend").status_code == 429
    with time_machine.travel(timezone.now() + timedelta(seconds=61)):
        response = admin.post(f"/api/v1/invitations/{sent.id}/resend")
        assert response.status_code == 200
        assert response.json()["send_count"] == 2
        new_token = last_token()
        assert new_token != sent.token
        assert (
            api_client.post("/api/v1/invitations/preview", {"token": sent.token}, format="json").status_code
            == 404
        )
        assert (
            api_client.post("/api/v1/invitations/preview", {"token": new_token}, format="json").status_code
            == 200
        )


def test_resend_revives_an_expired_invitation(world, invite, as_member):
    sent = invite()
    Invitation.objects.filter(pk=sent.id).update(
        expires_at=timezone.now() - timedelta(hours=1), last_sent_at=timezone.now() - timedelta(hours=80)
    )
    response = as_member(world.admin).post(f"/api/v1/invitations/{sent.id}/resend")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_revoke_is_terminal_and_never_removes_access(world, invite, accept, as_member):
    admin = as_member(world.admin)
    accepted = invite()
    assert accept(accepted).status_code == 200
    assert admin.post(f"/api/v1/invitations/{accepted.id}/revoke").status_code == 409
    assert Membership.objects.filter(school=world.school, user__phone=PHONE, is_active=True).exists()

    pending = invite(recipient="+919811100003", employee_id="T-3")
    assert admin.post(f"/api/v1/invitations/{pending.id}/revoke").json()["status"] == "revoked"
    assert admin.post(f"/api/v1/invitations/{pending.id}/revoke").status_code == 409
    assert admin.post(f"/api/v1/invitations/{pending.id}/resend").status_code == 409


def test_expiry_job_marks_overdue_invitations_in_every_school(world, other_world, invite, as_member):
    sent = invite()
    other = as_member(other_world.admin).post(
        "/api/v1/invitations",
        {
            "kind": "student",
            "channel": "phone",
            "recipient": "+919811100004",
            "full_name": "Other",
            "student_id": str(other_world.other_student.pk),
        },
        format="json",
    )
    assert other.status_code == 201
    with time_machine.travel(timezone.now() + timedelta(hours=73)):
        assert expire_due_invitations() == 2
    assert set(Invitation.objects.values_list("status", flat=True)) == {"expired"}
    assert AuditEvent.objects.filter(action="invitations.invitation.expired").count() == 2
    assert Invitation.objects.get(pk=sent.id).status == "expired"


def test_duplicate_pending_invitation_is_a_conflict(world, invite):
    invite()
    duplicate = invite(employee_id="T-DUP", expect=409)
    assert duplicate.body["error"]["code"] == "conflict"
    invite("student")
    assert invite("student", recipient="+919811100005", expect=409).body["error"]["code"] == "conflict"


# -------------------------------------------------------------------------------------------------- delivery
def test_disabled_channel_saves_nothing(world, invite, settings):
    settings.OTP_SMS_PROVIDER = "eduflow.identity.otp.providers.DisabledSmsProvider"
    assert invite(expect=503).body["error"]["code"] == "service_unavailable"
    settings.EMAIL_BACKEND = "eduflow.identity.delivery.DisabledEmailBackend"
    invite(channel="email", recipient="a@example.test", expect=503)
    assert not Invitation.objects.exists()


class FailingSmsProvider:
    def send(self, phone: str, message: str) -> None:
        from eduflow.identity.otp.providers import SmsUnavailable

        raise SmsUnavailable("gateway down")


def test_provider_failure_rolls_back_and_claims_nothing(world, invite, settings):
    settings.OTP_SMS_PROVIDER = "eduflow.invitations.tests.test_lifecycle.FailingSmsProvider"
    invite(expect=503)
    assert not Invitation.objects.exists()
    assert not AuditEvent.objects.filter(action="invitations.invitation.created").exists()
    assert MemorySmsProvider.outbox == []


def test_secrets_never_reach_logs_or_audit(world, invite, accept, json_logs):
    sent = invite()
    response = accept(sent)
    assert response.status_code == 200
    code = last_code()
    dump = json_logs.text + str(list(AuditEvent.objects.values("metadata", "target_id")))
    for secret in (sent.token, token_digest(sent.token), code, PHONE):
        assert secret not in dump
