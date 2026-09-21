from django.tasks import task

from .push import deliver


@task
def deliver_push(notification_ids: list[str]) -> dict:
    return deliver(notification_ids)
