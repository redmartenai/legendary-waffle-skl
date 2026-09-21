"""Push delivery through Expo's push service (FCM on Android, APNs on iOS)."""

import logging

import httpx
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import PushDevice
from apps.core.utils import parse_hhmm, school_now, time_in_window

from .models import ALWAYS_DELIVER, Category, Notification, Priority, PushStatus

logger = logging.getLogger("apps.notifications")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Android notification channels created by the app (see mobile/src/notifications).
CHANNEL_FOR_CATEGORY = {
    Category.BUS: "bus",
    Category.SAFETY: "safety",
    Category.CHAT: "chat",
}


def _in_quiet_hours(notification: Notification) -> bool:
    if notification.category in ALWAYS_DELIVER or notification.priority != Priority.NORMAL:
        return False
    user = notification.user
    school = notification.school
    if user.quiet_hours_start and user.quiet_hours_end:
        start, end = user.quiet_hours_start, user.quiet_hours_end
    else:
        window = (school.policy("notifications", "quiet_hours") if school else None) or settings.EDUFLOW[
            "DEFAULT_QUIET_HOURS"
        ]
        start, end = parse_hhmm(window[0]), parse_hhmm(window[1])
    now = school_now(school).time() if school else timezone.localtime().time()
    return time_in_window(now, start, end)


def _message(notification: Notification, token: str) -> dict:
    high = notification.category in ALWAYS_DELIVER or notification.priority != Priority.NORMAL
    return {
        "to": token,
        "title": notification.title,
        "body": notification.body,
        "data": {**notification.data, "notification_id": str(notification.id), "category": notification.category},
        "channelId": CHANNEL_FOR_CATEGORY.get(notification.category, "school"),
        "priority": "high" if high else "default",
        "sound": "default",
    }


def deliver(notification_ids: list[str]) -> dict:
    notifications = list(
        Notification.objects.select_related("user", "school").filter(
            id__in=notification_ids, push_status=PushStatus.PENDING
        )
    )
    counts = {"sent": 0, "quiet": 0, "no_device": 0, "skipped": 0, "failed": 0}
    outgoing: list[tuple[Notification, dict]] = []
    for notification in notifications:
        if _in_quiet_hours(notification):
            notification.push_status = PushStatus.QUIET
            counts["quiet"] += 1
            continue
        tokens = list(
            PushDevice.objects.filter(user=notification.user, is_active=True).values_list("token", flat=True)
        )
        if not tokens:
            notification.push_status = PushStatus.NO_DEVICE
            counts["no_device"] += 1
            continue
        if not settings.EDUFLOW["PUSH_ENABLED"]:
            notification.push_status = PushStatus.SKIPPED
            counts["skipped"] += 1
            continue
        outgoing.extend((notification, _message(notification, token)) for token in tokens)

    if outgoing:
        _send_to_expo(outgoing, counts)

    Notification.objects.bulk_update(notifications, ["push_status"])
    return counts


def _send_to_expo(outgoing: list[tuple[Notification, dict]], counts: dict) -> None:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if settings.EDUFLOW["EXPO_ACCESS_TOKEN"]:
        headers["Authorization"] = f"Bearer {settings.EDUFLOW['EXPO_ACCESS_TOKEN']}"
    for start in range(0, len(outgoing), 100):
        chunk = outgoing[start : start + 100]
        try:
            response = httpx.post(EXPO_PUSH_URL, json=[m for _, m in chunk], headers=headers, timeout=10)
            response.raise_for_status()
            tickets = response.json().get("data", [])
        except httpx.HTTPError as exc:
            logger.warning("Expo push request failed: %s", exc)
            for notification, _ in chunk:
                notification.push_status = PushStatus.FAILED
            counts["failed"] += len(chunk)
            continue
        for (notification, message), ticket in zip(chunk, tickets):
            if ticket.get("status") == "ok":
                notification.push_status = PushStatus.SENT
                counts["sent"] += 1
            else:
                notification.push_status = PushStatus.FAILED
                counts["failed"] += 1
                if ticket.get("details", {}).get("error") == "DeviceNotRegistered":
                    PushDevice.objects.filter(token=message["to"]).update(is_active=False)
