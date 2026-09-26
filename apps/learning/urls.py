from django.urls import path

from . import views

urlpatterns = [
    path("students/<uuid:student_id>/materials", views.StudentMaterialsView.as_view()),
    path("materials/<uuid:material_id>/file", views.MaterialFileView.as_view()),
    path("students/<uuid:student_id>/classes", views.StudentClassesView.as_view()),
    path("students/<uuid:student_id>/assignments", views.StudentAssignmentsView.as_view()),
    path("assignments/milestones/<uuid:progress_id>/toggle", views.MilestoneToggleView.as_view()),
    path("assignments/<uuid:assignment_id>/files", views.AssignmentUploadView.as_view()),
    path("teacher/assignments", views.TeacherAssignmentsView.as_view()),
    path("assignments/<uuid:assignment_id>/review", views.AssignmentReviewView.as_view()),
    path("assignments/submissions/<uuid:submission_id>/grade", views.SubmissionGradeView.as_view()),
    path("assignments/files/<uuid:file_id>", views.SubmissionFileView.as_view()),
]
