from django.urls import path

from . import views

urlpatterns = [
    path("parent/children", views.ParentChildrenView.as_view()),
    path("student/me", views.StudentSelfView.as_view()),
    path("students/<uuid:student_id>/summary", views.StudentSummaryView.as_view()),
    path("students/<uuid:student_id>/timetable", views.StudentTimetableView.as_view()),
    path("students/<uuid:student_id>/remarks", views.StudentRemarksView.as_view()),
    path("teacher/classes", views.TeacherClassesView.as_view()),
    path("teacher/today", views.TeacherTodayView.as_view()),
]
