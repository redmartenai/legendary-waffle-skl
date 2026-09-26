from django.urls import path

from . import views

urlpatterns = [
    path("approvals", views.ApprovalListView.as_view()),
    path("approvals/history", views.ApprovalHistoryView.as_view()),
    path("approvals/<uuid:request_id>/decide", views.ApprovalDecideView.as_view()),
    path("approvals/<uuid:request_id>/undo", views.ApprovalUndoView.as_view()),
    path("approvals/<uuid:request_id>/file", views.ApprovalFileView.as_view()),
]
