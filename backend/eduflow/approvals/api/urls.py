from django.urls import path

from . import views

urlpatterns = [
    path("approvals", views.ApprovalQueueView.as_view(), name="approval-queue"),
    path(
        "approvals/<slug:kind>/<uuid:pk>/decision",
        views.ApprovalDecisionView.as_view(),
        name="approval-decision",
    ),
]
