"""Tenant context in background jobs (docs/security/rls.md#background-jobs)."""

import pytest
from celery import shared_task

from eduflow.core import db_context
from eduflow.core.celery_context import add_request_id_header
from eduflow.core.db_context import DbContext
from eduflow.tenancy.models import Membership
from eduflow.tenancy.tasks import InactiveTenant, MissingTenantContext, TenantContextMismatch, TenantTask

pytestmark = pytest.mark.django_db


@shared_task(base=TenantTask, name="eduflow.tests.visible_memberships")
def visible_memberships(*, school_id: str) -> list[str]:
    # Deliberately unfiltered: only RLS limits what this sees.
    return sorted(str(s) for s in Membership.objects.values_list("school_id", flat=True))


@shared_task(name="eduflow.tests.untenanted_count")
def untenanted_count() -> int:
    return Membership.objects.count()


@pytest.fixture
def world(make_school, make_member):
    a, b = make_school("task-a"), make_school("task-b")
    make_member(a)
    make_member(a)
    make_member(b)
    return {"a": a, "b": b}


def test_tenant_task_sees_only_its_school(world):
    result = visible_memberships.delay(school_id=str(world["a"].pk)).get()
    assert result == [str(world["a"].pk)] * 2
    assert visible_memberships.delay(school_id=str(world["b"].pk)).get() == [str(world["b"].pk)]


def test_task_without_tenant_context_sees_nothing(world):
    assert Membership.objects.count() == 3
    assert untenanted_count.delay().get() == 0


def test_missing_school_id_fails(world):
    with pytest.raises(MissingTenantContext):
        visible_memberships.apply(kwargs={}).get()
    with pytest.raises(MissingTenantContext):
        visible_memberships.apply(kwargs={"school_id": "not-a-uuid"}).get()


def test_inactive_or_unknown_school_fails(world):
    world["b"].is_active = False
    world["b"].save()
    with pytest.raises(InactiveTenant):
        visible_memberships.apply(kwargs={"school_id": str(world["b"].pk)}).get()


def test_task_enqueued_by_school_a_cannot_run_for_school_b(world):
    with pytest.raises(TenantContextMismatch):
        visible_memberships.apply(
            kwargs={"school_id": str(world["b"].pk)}, headers={"school_id": str(world["a"].pk)}
        ).get()
    result = visible_memberships.apply(
        kwargs={"school_id": str(world["a"].pk)}, headers={"school_id": str(world["a"].pk)}
    ).get()
    assert len(result) == 2


def test_publisher_records_the_requests_school(world):
    with db_context.scoped(DbContext(school_id=world["a"].pk)):
        headers: dict[str, str] = {}
        add_request_id_header(headers=headers)
    assert headers["school_id"] == str(world["a"].pk)


def test_context_is_restored_after_an_inline_task(world):
    with db_context.scoped(DbContext(school_id=world["b"].pk)):
        visible_memberships.delay(school_id=str(world["b"].pk)).get()
        assert db_context.current() == DbContext(school_id=world["b"].pk)
        assert Membership.objects.count() == 1
    assert db_context.current() is None


def test_purge_task_removes_only_expired_records(make_user):
    from datetime import timedelta

    from django.utils import timezone

    from eduflow.identity.models import AuthMethod, AuthSession, OtpChallenge
    from eduflow.identity.tasks import purge_expired_auth_records
    from eduflow.identity.tokens import start_session

    user = make_user()
    live = start_session(user, method=AuthMethod.PASSWORD).session
    old = start_session(user, method=AuthMethod.PASSWORD).session
    AuthSession.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(days=40))
    OtpChallenge.objects.create(
        phone_hash="x", code_hash="y", expires_at=timezone.now() - timedelta(days=40), max_attempts=5
    )

    purge_expired_auth_records.delay().get()

    assert list(AuthSession.objects.values_list("pk", flat=True)) == [live.pk]
    assert not OtpChallenge.objects.exists()
