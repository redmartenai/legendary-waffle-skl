"""Input validation and failure paths of invitations: each refusal leaves nothing behind."""

from datetime import timedelta

import pytest
from django.utils import timezone

from eduflow.academics.models import Department
from eduflow.identity.models import OtpChallenge, User
from eduflow.invitations.models import Invitation
from eduflow.people.models import Guardian, StaffProfile, Student

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("channel", "recipient"), [("phone", "12"), ("email", "not-an-email"), ("email", "a@b")]
)
def test_invalid_recipients_are_rejected(world, invite, channel, recipient):
    response = invite(channel=channel, recipient=recipient, expect=400)
    assert "recipient" in response.body["error"]["fields"]


def test_student_and_guardian_invitations_carry_only_their_own_role(world, invite):
    teacher_role = str(world.teacher.role_assignments.get().role_id)
    response = invite("student", role_ids=[teacher_role], expect=400)
    assert "role_ids" in response.body["error"]["fields"]


def test_staff_invitations_need_roles_and_an_employee_id(world, invite):
    assert "role_ids" in invite(role_ids=[], expect=400).body["error"]["fields"]
    assert "employee_id" in invite(employee_id="", expect=400).body["error"]["fields"]
    assert invite(employee_id="T-1", expect=409).body["error"]["code"] == "conflict"  # the fixture teacher's


def test_staff_details_only_on_staff_invitations(world, invite):
    invite("student", employee_id="X-1", expect=400)


def test_archived_department_is_rejected(world, invite):
    old = Department.objects.create(school=world.school, name="Old", code="old", status="archived")
    assert "department_id" in invite(department_id=str(old.pk), expect=400).body["error"]["fields"]


def test_inactive_student_and_linked_guardian_cannot_be_invited(world, invite, make_member):
    Student.objects.filter(pk=world.other_student.pk).update(status="left")
    assert "student_id" in invite("student", expect=400).body["error"]["fields"]
    assert (
        "guardian_id" in invite("guardian", guardian_id=world.guardian.pk, expect=400).body["error"]["fields"]
    )


def test_guardian_linked_meanwhile_rolls_back(world, invite, accept, make_member):
    guardian = Guardian.objects.create(school=world.school, full_name="Unlinked")
    sent = invite("guardian", guardian_id=guardian.pk)
    Guardian.objects.filter(pk=guardian.pk).update(membership_id=make_member(world.school).pk)
    assert accept(sent).status_code == 409
    assert not User.objects.filter(phone="+919811100001").exists()


def test_staff_profile_created_meanwhile_rolls_back(world, invite, accept, make_member):
    sent = invite()
    StaffProfile.objects.create(
        school=world.school, membership=make_member(world.school), employee_id="T-NEW"
    )
    assert accept(sent).status_code == 409
    assert Invitation.objects.get(pk=sent.id).status == "pending"


def test_resend_and_verification_refuse_when_the_channel_is_down(
    world, invite, as_member, api_client, settings
):
    sent = invite()
    Invitation.objects.filter(pk=sent.id).update(last_sent_at=timezone.now() - timedelta(minutes=5))
    settings.OTP_SMS_PROVIDER = "eduflow.identity.otp.providers.DisabledSmsProvider"
    assert as_member(world.admin).post(f"/api/v1/invitations/{sent.id}/resend").status_code == 503
    verification = api_client.post("/api/v1/invitations/verification", {"token": sent.token}, format="json")
    assert verification.status_code == 503
    assert not OtpChallenge.objects.filter(subject_id=sent.id).exists()


def test_verification_delivery_failure_creates_no_challenge(world, invite, api_client, settings):
    sent = invite()
    settings.OTP_SMS_PROVIDER = "eduflow.invitations.tests.test_lifecycle.FailingSmsProvider"
    response = api_client.post("/api/v1/invitations/verification", {"token": sent.token}, format="json")
    assert response.status_code == 503
    assert not OtpChallenge.objects.filter(subject_id=sent.id).exists()


def test_inviter_who_lost_authority_cannot_resend(world, invite, as_member, make_member):
    from eduflow.authz.models import MembershipRole, Role
    from eduflow.authz.services import _set_grants, bump_rbac_version

    clerk = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key="resender", name="Resender")
    _set_grants(role, {"invitation.manage": ["school"], "invitation.read": ["school"]})
    MembershipRole.objects.create(school=world.school, membership=clerk, role=role)
    bump_rbac_version(world.school.pk)
    sent = invite()
    Invitation.objects.filter(pk=sent.id).update(last_sent_at=timezone.now() - timedelta(minutes=5))
    # invitation.manage alone does not cover a staff invitation's other permissions.
    assert as_member(clerk).post(f"/api/v1/invitations/{sent.id}/resend").status_code == 403


def test_signed_in_owner_of_unverified_address_gets_it_verified(world, invite, accept, make_user, client_for):
    user = make_user(phone="+919811100001")  # created by a school earlier, never verified
    assert accept(invite(), client=client_for(user)).status_code == 200
    user.refresh_from_db()
    assert user.phone_verified_at is not None


def test_expiry_task_runs_through_celery(world, invite):
    from eduflow.invitations.tasks import expire_due_invitations

    invite()
    Invitation.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    assert expire_due_invitations.delay().get() == 1
    assert Invitation.objects.get().status == "expired"


def test_disabled_email_backend_refuses_to_send():
    from django.core.mail import EmailMessage

    from eduflow.identity.delivery import DeliveryUnavailable, DisabledEmailBackend

    with pytest.raises(DeliveryUnavailable):
        DisabledEmailBackend().send_messages([EmailMessage("s", "b", to=["a@example.test"])])


def test_deliver_refuses_a_disabled_channel(settings):
    from eduflow.identity.delivery import DeliveryUnavailable, deliver

    settings.EMAIL_BACKEND = "eduflow.identity.delivery.DisabledEmailBackend"
    with pytest.raises(DeliveryUnavailable):
        deliver("email", "a@example.test", subject="s", body="b")
