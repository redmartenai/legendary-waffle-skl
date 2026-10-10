from django.urls import path

from . import views

urlpatterns = [
    path("notifications", views.NotificationListView.as_view(), name="notification-list"),
    path("notifications/read", views.NotificationReadView.as_view(), name="notification-read"),
    path("notifications/preferences", views.PreferenceView.as_view(), name="notification-preferences"),
]
