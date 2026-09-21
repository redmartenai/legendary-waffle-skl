from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response

from apps.core.api import SchoolAPIView

from .models import Notification


def notification_payload(notification: Notification) -> dict:
    return {
        "id": str(notification.id),
        "category": notification.category,
        "title": notification.title,
        "body": notification.body,
        "data": notification.data,
        "priority": notification.priority,
        "created_at": notification.created_at.isoformat(),
        "read": notification.read_at is not None,
    }


class NotificationListView(SchoolAPIView):
    def get(self, request):
        base = Notification.objects.filter(user=request.user).filter(Q(school=request.school) | Q(school__isnull=True))
        category = request.query_params.get("category")
        items = base.filter(category=category) if category else base
        return Response(
            {
                "unread": base.filter(read_at__isnull=True).count(),
                "items": [notification_payload(n) for n in items[:60]],
            }
        )


class NotificationReadView(SchoolAPIView):
    def post(self, request):
        base = Notification.objects.filter(user=request.user, read_at__isnull=True).filter(
            Q(school=request.school) | Q(school__isnull=True)
        )
        if not request.data.get("all"):
            base = base.filter(id__in=request.data.get("ids") or [])
        updated = base.update(read_at=timezone.now())
        return Response({"marked": updated})
