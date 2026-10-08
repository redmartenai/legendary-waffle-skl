from django.urls import path

from . import views

urlpatterns = [
    path("auth/password/login", views.PasswordLoginView.as_view(), name="auth-password-login"),
    path("auth/password/change", views.PasswordChangeView.as_view(), name="auth-password-change"),
    path("auth/otp/request", views.OtpRequestView.as_view(), name="auth-otp-request"),
    path("auth/otp/verify", views.OtpVerifyView.as_view(), name="auth-otp-verify"),
    path("auth/token/refresh", views.TokenRefreshView.as_view(), name="auth-token-refresh"),
    path("auth/logout", views.LogoutView.as_view(), name="auth-logout"),
    path("auth/logout-all", views.LogoutAllView.as_view(), name="auth-logout-all"),
    path("auth/sessions", views.SessionListView.as_view(), name="auth-sessions"),
    path("auth/sessions/<uuid:session_id>", views.SessionDetailView.as_view(), name="auth-session-detail"),
    path("me", views.MeView.as_view(), name="me"),
]
