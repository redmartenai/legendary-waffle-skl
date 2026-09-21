from django.urls import path

from . import views

urlpatterns = [
    path("students/<uuid:student_id>/homework", views.StudentHomeworkView.as_view()),
    path("homework/<uuid:homework_id>/submissions", views.HomeworkSubmitView.as_view()),
    path("homework/<uuid:homework_id>/submissions/all", views.HomeworkSubmissionsView.as_view()),
    path("homework/submissions/<uuid:submission_id>/review", views.SubmissionReviewView.as_view()),
    path("classes/<uuid:class_id>/homework", views.ClassHomeworkView.as_view()),
]
