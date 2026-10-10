"""Notifications: recipients, the inbox, read state, opt-in delivery with explicit status, isolation, RLS."""

import pytest
from django.core import mail

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.notifications import services
from eduflow.notifications.models import DeliveryStatus, Notification, NotificationDelivery

pytestmark = pytest.mark.django_db


def test_family_recipients_are_the_childs_guardians_and_the_student(world):
    members = services.family_of(world.student)
    assert {m.pk for m in members} == {world.parent.pk, world.student_member.pk}
    assert services.family_of(world.other_student) == []  # no linked accounts


def test_notify_creates_one_notification_per_recipient_and_ignores_other_schools(world, other_world):
    created = services.notify(
        world.school,
        [world.parent, world.parent, other_world.parent],
        kind="homework",
        title="Maths homework",
        student=world.student,
        link=("homework", "abc"),
    )
    assert len(created) == 1
    assert Notification.objects.get().recipient_id == world.parent.pk


def test_the_inbox_is_personal_and_can_be_marked_read(world, as_member):
    services.notify(world.school, [world.parent], kind="fees", title="Fee reminder")
    services.notify(world.school, [world.teacher], kind="alert", title="Register not marked")
    parent = as_member(world.parent)
    rows = parent.get("/api/v1/notifications").json()["results"]
    assert [r["title"] for r in rows] == ["Fee reminder"]
    assert rows[0]["read"] is False
    assert parent.post("/api/v1/notifications/read", {}, format="json").json() == {"updated": 1}
    assert parent.get("/api/v1/notifications?unread=true").json()["results"] == []
    assert as_member(world.teacher).get("/api/v1/notifications").json()["results"][0]["read"] is False


def test_external_delivery_is_opt_in_and_reports_real_status(
    world, as_member, settings, django_capture_on_commit_callbacks
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    world.parent.user.email = "parent@example.test"
    world.parent.user.save()
    services.notify(world.school, [world.parent], kind="fees", title="Not opted in")
    assert not NotificationDelivery.objects.exists()
    as_member(world.parent).put(
        "/api/v1/notifications/preferences",
        {
            "preferences": [
                {"kind": "fees", "channel": "email", "enabled": True},
                {"kind": "fees", "channel": "sms", "enabled": True},
            ]
        },
        format="json",
    )
    with django_capture_on_commit_callbacks(execute=True):
        services.notify(world.school, [world.parent], kind="fees", title="Fee reminder", body="Due on 15 Oct")
    statuses = dict(NotificationDelivery.objects.values_list("channel", "status"))
    assert statuses["email"] == DeliveryStatus.SENT
    assert mail.outbox[-1].subject == "Fee reminder"
    assert statuses["sms"] == DeliveryStatus.SUPPRESSED  # the parent has no phone number
    assert (
        services.send(NotificationDelivery.objects.get(channel="email").pk) == DeliveryStatus.SENT
    )  # idempotent


def test_a_disabled_channel_is_suppressed_not_sent(world, settings, django_capture_on_commit_callbacks):
    settings.EMAIL_BACKEND = "eduflow.identity.delivery.DisabledEmailBackend"
    from eduflow.notifications.models import NotificationPreference

    NotificationPreference.objects.create(
        school=world.school, membership=world.parent, kind="alert", channel="email"
    )
    with django_capture_on_commit_callbacks(execute=True):
        services.notify(world.school, [world.parent], kind="alert", title="X")
    delivery = NotificationDelivery.objects.get()
    assert (delivery.status, delivery.reason) == ("suppressed", "channel_not_configured")


def test_preferences_are_validated_and_personal(world, as_member):
    bad = as_member(world.parent).put(
        "/api/v1/notifications/preferences",
        {"preferences": [{"kind": "fees", "channel": "whatsapp", "enabled": True}]},
        format="json",
    )
    assert bad.status_code == 400
    assert as_member(world.teacher).get("/api/v1/notifications/preferences").json() == {"preferences": []}


def test_notifications_are_under_rls(world, other_world):
    services.notify(other_world.school, [other_world.parent], kind="fees", title="B only")
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Notification.objects.exists()
    with db_context.scoped(DbContext()):
        assert not Notification.objects.exists()
