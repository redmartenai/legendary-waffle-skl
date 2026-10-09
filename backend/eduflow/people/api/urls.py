from django.urls import path

from . import views

urlpatterns = [
    path("staff", views.StaffList.as_view(), name="staff-list"),
    path("staff/<uuid:pk>", views.StaffDetail.as_view(), name="staff-detail"),
    path("students", views.StudentList.as_view(), name="student-list"),
    path("students/<uuid:pk>", views.StudentDetail.as_view(), name="student-detail"),
    path("guardians", views.GuardianList.as_view(), name="guardian-list"),
    path("guardians/<uuid:pk>", views.GuardianDetail.as_view(), name="guardian-detail"),
    path("student-guardians", views.StudentGuardianList.as_view(), name="student-guardian-list"),
    path(
        "student-guardians/<uuid:pk>", views.StudentGuardianDetail.as_view(), name="student-guardian-detail"
    ),
    path("enrollments", views.EnrollmentList.as_view(), name="enrollment-list"),
    path("enrollments/<uuid:pk>", views.EnrollmentDetail.as_view(), name="enrollment-detail"),
    path("enrollments/<uuid:pk>/end", views.EnrollmentEndView.as_view(), name="enrollment-end"),
    path(
        "enrollments/<uuid:pk>/transfer", views.EnrollmentTransferView.as_view(), name="enrollment-transfer"
    ),
    path("teacher-assignments", views.TeacherAssignmentList.as_view(), name="teacher-assignment-list"),
    path(
        "teacher-assignments/<uuid:pk>",
        views.TeacherAssignmentDetail.as_view(),
        name="teacher-assignment-detail",
    ),
]
