import logging

from django.db import transaction

from .models import Notification, Priority

logger = logging.getLogger("apps.notifications")


def notify(
    users,
    *,
    school,
    category: str,
    title: str,
    body: str,
    data: dict | None = None,
    dedupe_key: str | None = None,
    priority: str = Priority.NORMAL,
    push: bool = True,
) -> list[Notification]:
    """Create inbox notifications and queue push delivery after the transaction commits.

    ``dedupe_key`` makes the call idempotent per user: the same event is never
    delivered twice to the same person, however many times it is triggered.
    """
    unique_users = {user.id: user for user in users if user is not None and user.is_active}
    if not unique_users:
        return []

    already = set()
    if dedupe_key:
        already = set(
            Notification.objects.filter(user_id__in=unique_users.keys(), dedupe_key=dedupe_key).values_list(
                "user_id", flat=True
            )
        )
    fresh = [
        Notification(
            user=user,
            school=school,
            category=category,
            title=title[:140],
            body=body[:500],
            data=data or {},
            priority=priority,
            dedupe_key=dedupe_key,
        )
        for user_id, user in unique_users.items()
        if user_id not in already
    ]
    if not fresh:
        return []

    Notification.objects.bulk_create(fresh, ignore_conflicts=True)
    # With ignore_conflicts a concurrent duplicate is skipped silently; only push what was stored.
    stored_ids = list(Notification.objects.filter(id__in=[n.id for n in fresh]).values_list("id", flat=True))
    created = [n for n in fresh if n.id in set(stored_ids)]

    if push and created:
        ids = [str(n.id) for n in created]

        def _enqueue():
            from .tasks import deliver_push

            deliver_push.enqueue(ids)

        transaction.on_commit(_enqueue)
    elif created:
        Notification.objects.filter(id__in=stored_ids).update(push_status="skipped")
    return created
