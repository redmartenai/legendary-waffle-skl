from django.urls import path

from . import views

urlpatterns = [
    path("classes/<uuid:class_id>/roster", views.ClassRosterView.as_view()),
    path("classes/<uuid:class_id>/attendance", views.ClassAttendanceView.as_view()),
    path("students/<uuid:student_id>/attendance", views.StudentAttendanceView.as_view()),
]
