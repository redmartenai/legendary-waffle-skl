"""All versioned API routes (/api/v1/...). Web, mobile and driver apps share them."""

from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts import views as account_views
from apps.notifications import views as notification_views
from apps.realtime import views as realtime_views
from apps.tenancy import views as tenancy_views

urlpatterns = [
    path("schools/lookup", tenancy_views.SchoolLookupView.as_view()),
    path("auth/otp/request", account_views.OtpRequestView.as_view()),
    path("auth/otp/verify", account_views.OtpVerifyView.as_view()),
    path("auth/token/refresh", TokenRefreshView.as_view()),
    path("me", account_views.MeView.as_view()),
    path("me/push-devices", account_views.PushDeviceView.as_view()),
    path("notifications", notification_views.NotificationListView.as_view()),
    path("notifications/read", notification_views.NotificationReadView.as_view()),
    path("realtime/connection", realtime_views.RealtimeConnectionView.as_view()),
    path("", include("apps.academics.urls")),
    path("", include("apps.attendance.urls")),
    path("", include("apps.homework.urls")),
    path("", include("apps.fees.urls")),
    path("", include("apps.results.urls")),
    path("", include("apps.announcements.urls")),
    path("", include("apps.transport.urls")),
    path("", include("apps.messaging.urls")),
]
