"""Creating notifications and delivering them on opted-in external channels.

Modules call :func:`notify` inside their own transactions; the external deliveries are queued only after
the transaction commits (so a rolled-back action sends nothing). Recipients are always memberships of the
same school; helpers resolve the people a record concerns (a student's guardians, the student, a teacher).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from eduflow.core.logging import get_logger
from eduflow.identity import delivery
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, School

from .models import Channel, DeliveryStatus, Notification, NotificationDelivery, NotificationPreference

log = get_logger(__name__)


# --------------------------------------------------------------------------------------------- recipients
def guardians_of(student: Student) -> list[Membership]:
    """Active memberships of the student's linked guardians (only those who sign in)."""
    return list(
        Membership.objects.filter(
            guardian_profile__student_links__student=student, is_active=True, school_id=student.school_id
        ).distinct()
    )


def student_member(student: Student) -> list[Membership]:
    member = student.membership
    return [member] if member is not None and member.is_active else []


def family_of(student: Student) -> list[Membership]:
    return [*guardians_of(student), *student_member(student)]


# --------------------------------------------------------------------------------------------- notify
def notify(
    school: School | Any,
    recipients: Iterable[Membership],
    *,
    kind: str,
    title: str,
    body: str = "",
    student: Student | None = None,
    link: tuple[str, Any] | None = None,
) -> list[Notification]:
    """One in-app notification per recipient (deduplicated), plus queued opted-in deliveries."""
    seen: set[Any] = set()
    created = []
    for member in recipients:
        if member is None or member.pk in seen or member.school_id != getattr(school, "pk", school):
            continue
        seen.add(member.pk)
        created.append(
            Notification(
                school_id=member.school_id,
                recipient=member,
                kind=kind,
                title=title[:200],
                body=body[:1000],
                student=student,
                link_type=link[0] if link else "",
                link_id=str(link[1]) if link else "",
            )
        )
    if not created:
        return []
    Notification.objects.bulk_create(created)
    _queue_deliveries(created, kind)
    return created


def _queue_deliveries(notifications: list[Notification], kind: str) -> None:
    opted = set(
        NotificationPreference.objects.filter(
            membership_id__in=[n.recipient_id for n in notifications], kind=kind, enabled=True
        ).values_list("membership_id", "channel")
    )
    deliveries = [
        NotificationDelivery(school_id=n.school_id, notification=n, channel=channel)
        for n in notifications
        for channel in Channel.values
        if (n.recipient_id, channel) in opted
    ]
    if not deliveries:
        return
    NotificationDelivery.objects.bulk_create(deliveries)
    ids = [(str(d.pk), str(d.school_id)) for d in deliveries]

    def enqueue() -> None:
        from .tasks import deliver_notification

        for delivery_id, school_id in ids:
            deliver_notification.delay(delivery_id=delivery_id, school_id=school_id)

    transaction.on_commit(enqueue)


def send(delivery_id: Any) -> str:
    """Attempt one delivery. Idempotent: anything not ``queued`` is left as it is."""
    with transaction.atomic():
        item = (
            NotificationDelivery.objects.select_for_update(of=("self",))
            .select_related("notification__recipient__user")
            .filter(pk=delivery_id)
            .first()
        )
        if item is None or item.status != DeliveryStatus.QUEUED:
            return item.status if item else "missing"
        user = item.notification.recipient.user
        channel = delivery.Channel.EMAIL if item.channel == Channel.EMAIL else delivery.Channel.PHONE
        address = user.email if item.channel == Channel.EMAIL else user.phone
        item.attempts += 1
        if not delivery.channel_enabled(channel):
            item.status, item.reason = DeliveryStatus.SUPPRESSED, "channel_not_configured"
        elif not address:
            item.status, item.reason = DeliveryStatus.SUPPRESSED, "no_address"
        else:
            note = item.notification
            try:
                delivery.deliver(
                    channel, address, subject=note.title, body=f"{note.title}\n\n{note.body}".strip()
                )
            except delivery.DeliveryUnavailable:
                item.status, item.reason = DeliveryStatus.FAILED, "provider_error"
                log.warning("notification_delivery_failed", channel=item.channel)
            else:
                item.status, item.reason = DeliveryStatus.SENT, ""
        item.save(update_fields=["status", "attempts", "reason", "updated_at"])
        return str(item.status)


# --------------------------------------------------------------------------------------------- inbox
def inbox(member: Membership) -> QuerySet[Notification]:
    return Notification.objects.filter(recipient=member).select_related("student").order_by("-created_at")


def mark_read(member: Membership, notification_ids: Iterable[Any] | None = None) -> int:
    rows = Notification.objects.filter(recipient=member, read_at__isnull=True)
    if notification_ids is not None:
        rows = rows.filter(pk__in=list(notification_ids))
    return rows.update(read_at=timezone.now())


def set_preferences(member: Membership, preferences: Iterable[dict[str, Any]]) -> None:
    with transaction.atomic():
        for pref in preferences:
            NotificationPreference.objects.update_or_create(
                school_id=member.school_id,
                membership=member,
                kind=pref["kind"],
                channel=pref["channel"],
                defaults={"enabled": pref["enabled"]},
            )
