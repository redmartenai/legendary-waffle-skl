"""PostgreSQL Row-Level Security, tested directly at the database layer (ADR-018, docs/security/rls.md).

These tests deliberately bypass the application's own filters (plain ``Model.objects.all()``), because RLS is
the layer that must still hold when application code forgets one.
"""

import uuid

import pytest
from django.conf import settings
from django.db import DatabaseError, ProgrammingError, connection, transaction

from eduflow.audit.models import AuditEvent
from eduflow.authz.models import MembershipRole, Role
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.tenancy.models import Membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(make_school, make_member):
    a, b = make_school("rls-a"), make_school("rls-b")
    return {"a": a, "b": b, "ma": make_member(a, roles=["teacher"]), "mb": make_member(b, roles=["teacher"])}


def _current_user():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, current_setting('eduflow.school_id', true)")
        return cursor.fetchone()


def test_requests_run_as_the_app_role_and_tasks_too():
    assert settings.DATABASE_RLS_ROLE == "eduflow_app"
    with db_context.scoped(DbContext()):
        assert _current_user()[0] == "eduflow_app"
    assert _current_user()[0] != "eduflow_app"


def test_missing_tenant_context_sees_no_rows(world):
    assert Membership.objects.count() == 2  # as owner (no context engaged): RLS does not apply
    with db_context.scoped(DbContext()):
        assert Membership.objects.count() == 0
        assert Role.objects.count() == 0
        assert MembershipRole.objects.count() == 0


def test_tenant_context_sees_only_its_school(world):
    with db_context.scoped(DbContext(school_id=world["a"].pk)):
        assert set(Membership.objects.values_list("school_id", flat=True)) == {world["a"].pk}
        assert set(Role.objects.values_list("school_id", flat=True)) == {world["a"].pk}
        # Even an explicit lookup of B's row by primary key finds nothing.
        assert not Membership.objects.filter(pk=world["mb"].pk).exists()


def test_user_context_reads_own_memberships_across_schools_only(world, make_member):
    user = world["ma"].user
    make_member(world["b"], user, roles=["parent"])
    with db_context.scoped(DbContext(user_id=user.pk)):
        assert set(Membership.objects.values_list("user_id", flat=True)) == {user.pk}
        assert Membership.objects.count() == 2
        assert set(Role.objects.values_list("key", flat=True)) == {"teacher", "parent"}


def test_user_context_is_read_only(world):
    with db_context.scoped(DbContext(user_id=world["ma"].user_id)):
        # The own-membership policy is SELECT-only: updates match nothing, inserts are refused.
        assert Membership.objects.filter(pk=world["ma"].pk).update(is_active=False) == 0
        with pytest.raises(ProgrammingError, match="row-level security"), transaction.atomic():
            Membership.objects.create(school=world["b"], user=world["ma"].user)
    world["ma"].refresh_from_db()
    assert world["ma"].is_active is True


def test_cannot_insert_into_another_school(world):
    with db_context.scoped(DbContext(school_id=world["a"].pk)):
        with pytest.raises(ProgrammingError, match="row-level security"), transaction.atomic():
            Membership.objects.create(school=world["b"], user=world["ma"].user)
        with pytest.raises(ProgrammingError, match="row-level security"), transaction.atomic():
            Role.objects.create(school=world["b"], key="injected", name="Injected")


def test_cannot_update_or_delete_another_school(world):
    with db_context.scoped(DbContext(school_id=world["a"].pk)):
        assert Membership.objects.filter(pk=world["mb"].pk).update(is_active=False) == 0
        assert Role.objects.filter(school=world["b"]).delete()[0] == 0
    world["mb"].refresh_from_db()
    assert world["mb"].is_active is True
    assert Role.objects.filter(school=world["b"]).exists()


def test_cannot_move_a_row_to_another_school(world):
    with (
        db_context.scoped(DbContext(school_id=world["a"].pk)),
        pytest.raises(ProgrammingError, match="row-level security"),
        transaction.atomic(),
    ):
        Membership.objects.filter(pk=world["ma"].pk).update(school=world["b"])


def test_bypass_is_explicit_and_scoped(world):
    with db_context.scoped(DbContext(school_id=world["a"].pk)):
        with db_context.system_context("test"):
            assert Membership.objects.count() == 2
        assert Membership.objects.count() == 1


def test_composite_foreign_keys_block_cross_school_assignment(world):
    role_b = Role.objects.get(school=world["b"], key="teacher")
    with pytest.raises(DatabaseError), transaction.atomic():  # as owner: RLS off, constraints still apply
        MembershipRole.objects.create(school=world["a"], membership=world["ma"], role=role_b)
    with pytest.raises(DatabaseError), transaction.atomic():
        MembershipRole.objects.create(school=world["b"], membership=world["ma"], role=role_b)


def test_audit_trail_is_tenant_isolated(world):
    AuditEvent.objects.create(action="x.a", school_id=world["a"].pk)
    AuditEvent.objects.create(action="x.b", school_id=world["b"].pk)
    AuditEvent.objects.create(action="x.platform", school_id=None)
    with db_context.scoped(DbContext(school_id=world["a"].pk)):
        assert set(AuditEvent.objects.values_list("action", flat=True)) == {"x.a"}
        AuditEvent.objects.create(action="ok.null", school_id=None)
        AuditEvent.objects.create(action="ok.own", school_id=world["a"].pk)
        with pytest.raises(ProgrammingError, match="row-level security"), transaction.atomic():
            AuditEvent.objects.create(action="forged", school_id=world["b"].pk)


@pytest.mark.parametrize("engaged", [False, True])
def test_audit_trail_is_append_only(world, engaged):
    event = AuditEvent.objects.create(action="x", school_id=world["a"].pk)
    ctx = DbContext(school_id=world["a"].pk) if engaged else None
    with db_context.scoped(ctx) if ctx else _nullcontext():
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).delete()
    event.refresh_from_db()
    assert event.action == "x"


def _nullcontext():
    from contextlib import nullcontext

    return nullcontext()


def test_request_context_is_released_after_the_request(world, client_for):
    client = client_for(world["ma"].user, world["a"])
    assert client.get("/api/v1/school").status_code == 200
    user, school = _current_user()
    assert user != "eduflow_app"
    assert school in ("", None)
    assert db_context.current() is None


def test_request_context_is_released_after_an_error(world, client_for):
    client = client_for(world["ma"].user, world["b"])  # not a member of B -> 403 inside the request
    assert client.get("/api/v1/school").status_code == 403
    assert _current_user()[0] != "eduflow_app"
    assert db_context.current() is None


def test_tenant_header_never_reaches_the_database_unverified(world, client_for, monkeypatch):
    seen = []
    original = db_context._write

    def spy(conn, ctx):
        seen.append(ctx)
        original(conn, ctx)

    monkeypatch.setattr(db_context, "_write", spy)
    client = client_for(world["ma"].user)
    client.get("/api/v1/school", HTTP_X_SCHOOL_ID=str(world["b"].pk))
    client.get("/api/v1/school", HTTP_X_SCHOOL_ID=str(uuid.uuid4()))
    assert all(c is None or c.school_id is None for c in seen)


@pytest.mark.django_db(transaction=True)  # a real reconnect cannot happen inside the per-test transaction
def test_reconnect_mid_request_reapplies_the_context(world):
    with db_context.scoped(DbContext(school_id=world["a"].pk)):
        connection.close()  # e.g. a dropped connection, re-opened on the next query
        assert _current_user() == ("eduflow_app", str(world["a"].pk))
        assert Membership.objects.count() == 1
