from django.urls import path

from . import views

urlpatterns = [
    path("homework", views.HomeworkList.as_view(), name="homework-list"),
    path("homework/<uuid:pk>", views.HomeworkDetail.as_view(), name="homework-detail"),
    path("homework/<uuid:pk>/submissions", views.HomeworkBoardView.as_view(), name="homework-board"),
    path("homework/<uuid:pk>/submit", views.HomeworkSubmitView.as_view(), name="homework-submit"),
    path("homework/<uuid:pk>/attachment", views.HomeworkAttachmentView.as_view(), name="homework-attachment"),
    path(
        "homework/submissions/<uuid:pk>/review", views.SubmissionReviewView.as_view(), name="homework-review"
    ),
    path("homework/submissions/<uuid:pk>/file", views.SubmissionFileView.as_view(), name="homework-file"),
]
