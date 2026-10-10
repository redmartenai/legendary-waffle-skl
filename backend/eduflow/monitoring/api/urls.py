from django.urls import path

from . import views

urlpatterns = [
    path("monitoring/alerts", views.AlertList.as_view(), name="alert-list"),
    path("monitoring/alerts/<uuid:pk>", views.AlertDetail.as_view(), name="alert-detail"),
    path("monitoring/alerts/<uuid:pk>/acknowledge", views.AlertAcknowledgeView.as_view(), name="alert-ack"),
    path("monitoring/alerts/<uuid:pk>/resolve", views.AlertResolveView.as_view(), name="alert-resolve"),
    path("monitoring/evaluate", views.EvaluateView.as_view(), name="monitoring-evaluate"),
    path("monitoring/rules", views.RulesView.as_view(), name="monitoring-rules"),
    path("monitoring/settings", views.SettingsView.as_view(), name="monitoring-settings"),
    path("monitoring/pulse", views.PulseView.as_view(), name="monitoring-pulse"),
    path("monitoring/risk", views.RiskView.as_view(), name="monitoring-risk"),
    path("students/<uuid:pk>/risk", views.StudentRiskView.as_view(), name="student-risk"),
    path("monitoring/scorecards", views.ScorecardsView.as_view(), name="monitoring-scorecards"),
    path("monitoring/ask", views.AskView.as_view(), name="monitoring-ask"),
]
