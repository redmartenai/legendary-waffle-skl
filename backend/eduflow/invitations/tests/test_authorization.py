"""Who may invite, what an accepted invitation grants, and that nothing crosses schools or roles."""

import threading

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from eduflow.authz.models import MembershipRole, Role
from eduflow.authz.services import _set_grants, bump_rbac_version
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.invitations.models import Invitation, InvitationRole
from eduflow.people.models import Guardian, Student, StudentGuardian
from eduflow.tenancy.models import Membership

from .conftest import last_code

pytestmark = pytest.mark.django_db


def _ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


# -------------------------------------------------------------------------------------------- who may invite
@pytest.mark.parametrize("role", ["teacher", "parent", "student_member"])
def test_non_admin_roles_cannot_see_or_send_invitations(world, invite, as_member, role):
    client = as_member(getattr(world, role))
    assert client.get("/api/v1/invitations").status_code == 403
    assert client.post("/api/v1/invitations", {}, format="json").status_code == 403


def test_principal_can_invite_but_not_beyond_their_own_powers(world, invite):
    invite(by=world.principal)  # a teacher: within the principal's grants
    admin_role = Role.objects.get(school=world.school, key="school_admin")
    denied = invite(
        by=world.principal,
        recipient="+919811100002",
        employee_id="A-1",
        role_ids=[str(admin_role.pk)],
        expect=403,
    )
    assert denied.body["error"]["code"] == "permission_denied"


def test_custom_role_with_narrow_invitation_scope_cannot_invite(world, invite, make_member):
    clerk = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key="clerk", name="Clerk")
    _set_grants(role, {"invitation.manage": ["section"], "invitation.read": ["school"]})
    MembershipRole.objects.create(school=world.school, membership=clerk, role=role)
    bump_rbac_version(world.school.pk)
    invite(by=clerk, expect=403)


def test_invitation_manage_alone_is_not_enough(world, invite, make_member):
    """Inviting also needs school-wide user.create, user.update and the target's write permission."""
    clerk = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key="inviter", name="Inviter")
    _set_grants(role, {"invitation.manage": ["school"], "invitation.read": ["school"]})
    MembershipRole.objects.create(school=world.school, membership=clerk, role=role)
    bump_rbac_version(world.school.pk)
    invite("student", by=clerk, expect=403)


def test_cannot_invite_yourself(world, invite):
    world.admin.user.phone = "+919811100077"
    world.admin.user.save()
    invite(recipient="+919811100077", expect=400)


def test_inviter_accepting_their_own_invitation_is_refused(world, invite, accept, client_for):
    sent = invite()
    admin_user = world.admin.user
    response = accept(sent, client=client_for(admin_user))
    # Even with a valid code for the invited address, the inviter cannot be the one who accepts.
    assert response.status_code == 400
    assert not Membership.objects.filter(user=admin_user).exclude(pk=world.admin.pk).exists()


# ----------------------------------------------------------------------------------------- inviter authority
def test_invitation_dies_with_the_inviters_authority(world, invite, accept, make_member):
    second_admin = make_member(world.school, roles=["school_admin"])
    sent = invite(by=second_admin)
    Membership.objects.filter(pk=second_admin.pk).update(is_active=False)
    bump_rbac_version(world.school.pk)
    response = accept(sent)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invitation_invalid"
    assert Invitation.objects.get(pk=sent.id).status == "pending"
    assert not Membership.objects.filter(user__phone="+919811100001").exists()


def test_role_removed_from_inviter_voids_role_grant(world, invite, accept, make_member):
    second_admin = make_member(world.school, roles=["school_admin"])
    sent = invite(by=second_admin)
    MembershipRole.objects.filter(membership=second_admin).delete()
    bump_rbac_version(world.school.pk)
    assert accept(sent).status_code == 404


# ---------------------------------------------------------------------------------- guardian / student scope
def test_guardian_invitation_gives_access_to_linked_children_only(world, invite, accept, api_client):
    guardian = Guardian.objects.create(school=world.school, full_name="Ravi's father", phone="+919811100001")
    StudentGuardian.objects.create(
        school=world.school, student=world.other_student, guardian=guardian, relationship="father"
    )
    sent = invite("guardian", guardian_id=guardian.pk)
    session = accept(sent).json()["session"]
    client = api_client
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {session['access']}", HTTP_X_SCHOOL_ID=str(world.school.pk)
    )
    assert _ids(client.get("/api/v1/students")) == {str(world.other_student.pk)}
    assert client.get(f"/api/v1/students/{world.student.pk}").status_code == 404
    guardian.refresh_from_db()
    assert guardian.membership is not None


def test_student_invitation_gives_self_access_only(world, invite, accept, api_client):
    session = accept(invite("student")).json()["session"]
    api_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {session['access']}", HTTP_X_SCHOOL_ID=str(world.school.pk)
    )
    assert _ids(api_client.get("/api/v1/students")) == {str(world.other_student.pk)}
    assert _ids(api_client.get("/api/v1/enrollments")) == {str(world.other_enrollment.pk)}


def test_record_linked_meanwhile_rolls_everything_back(world, invite, accept, make_member):
    sent = invite("student")
    Student.objects.filter(pk=world.other_student.pk).update(membership_id=make_member(world.school).pk)
    response = accept(sent)
    assert response.status_code == 409
    assert not Membership.objects.filter(user__phone="+919811100001").exists()
    assert Invitation.objects.get(pk=sent.id).status == "pending"


def test_cannot_invite_for_an_already_linked_record(world, invite):
    invite("student", student_id=world.student.pk, expect=400)


# --------------------------------------------------------------------------------------------------- schools
def test_acceptance_grants_roles_in_the_inviting_school_only(
    world, other_world, invite, accept, make_user, make_member, client_for
):
    from django.utils import timezone

    user = make_user(phone="+919811100001", phone_verified_at=timezone.now())
    make_member(other_world.school, user, roles=["parent"])
    assert accept(invite(), client=client_for(user)).status_code == 200
    in_a = client_for(user, world.school).get("/api/v1/me/permissions").json()["permissions"]
    in_b = client_for(user, other_world.school).get("/api/v1/me/permissions").json()["permissions"]
    assert "attendance.create" in in_a
    assert "attendance.create" not in in_b


def test_cross_school_references_are_rejected(world, other_world, invite):
    invite("student", student_id=other_world.other_student.pk, expect=400)
    invite(role_ids=[str(Role.objects.get(school=other_world.school, key="teacher").pk)], expect=400)
    invite(department_id=str(other_world.department.pk), expect=400)


INVITATION_MATRIX = [
    ("get", "/api/v1/invitations/{invitation}"),
    ("post", "/api/v1/invitations/{invitation}/resend"),
    ("post", "/api/v1/invitations/{invitation}/revoke"),
]
PHASE4_MATRIX_PATHS = {path for _, path in INVITATION_MATRIX}


@pytest.mark.parametrize(("method", "path"), INVITATION_MATRIX)
def test_school_a_cannot_touch_school_b_invitations(world, other_world, as_member, method, path):
    import uuid

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
    client = as_member(world.admin)
    foreign = getattr(client, method)(path.format(invitation=other.json()["id"]))
    missing = getattr(client, method)(path.format(invitation=uuid.uuid4()))
    assert foreign.status_code == missing.status_code == 404
    assert Invitation.objects.get(pk=other.json()["id"]).status == "pending"
    assert {r["id"] for r in client.get("/api/v1/invitations").json()["results"]} == set()


# ------------------------------------------------------------------------------------------------------- RLS
def test_invitation_tables_are_under_rls(world, other_world, invite):
    invite()
    with db_context.scoped(DbContext()):
        assert not Invitation.objects.exists()
        assert not InvitationRole.objects.exists()
    with db_context.scoped(DbContext(school_id=other_world.school.pk)):
        assert not Invitation.objects.exists()
        assert Invitation.objects.filter(school=world.school).update(status="revoked") == 0
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert Invitation.objects.count() == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_policies "
            "WHERE tablename IN ('invitations_invitation', 'invitations_invitation_role')"
        )
        assert cursor.fetchone()[0] == 2


# ----------------------------------------------------------------------------------------------- concurrency
@pytest.mark.django_db(transaction=True)
def test_parallel_acceptance_succeeds_once(world, invite, verify, client_for):
    from eduflow.invitations import services

    sent = invite()
    challenge = verify(sent)
    code = last_code()
    outcomes: list[str] = []

    def attempt() -> None:
        try:
            services.accept_invitation(sent.token, challenge_id=challenge, code=code, signed_in=None)
            outcomes.append("accepted")
        except Exception as exc:  # each thread reports what stopped it
            outcomes.append(type(exc).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("accepted") == 1, outcomes
    assert Membership.objects.filter(user__phone="+919811100001").count() == 1


# --------------------------------------------------------------------------------------------------- queries
def test_invitation_list_query_count_is_flat(world, invite, as_member):
    client = as_member(world.admin)
    invite()
    client.get("/api/v1/invitations")
    with CaptureQueriesContext(connection) as small:
        client.get("/api/v1/invitations")
    for n in range(2, 8):
        invite(recipient=f"+9198111000{n:02d}", employee_id=f"T-{n}")
    with CaptureQueriesContext(connection) as large:
        rows = client.get("/api/v1/invitations").json()["results"]
    assert len(rows) == 7
    assert len(large) == len(small)
