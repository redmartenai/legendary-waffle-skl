from django.urls import path

from . import views

urlpatterns = [
    # Client paths (CURRENT_STATE §5): "class" is a homeroom section (ADR-007).
    path("classes/<uuid:pk>/roster", views.ClassRosterView.as_view(), name="class-roster"),
    path("classes/<uuid:pk>/attendance", views.ClassAttendanceView.as_view(), name="class-attendance"),
    path("students/<uuid:pk>/attendance", views.StudentAttendanceView.as_view(), name="student-attendance"),
    # Canonical resources (ADR-006).
    path("attendance/sessions", views.SessionList.as_view(), name="attendance-session-list"),
    path("attendance/sessions/<uuid:pk>", views.SessionDetail.as_view(), name="attendance-session-detail"),
    path("attendance/records", views.RecordList.as_view(), name="attendance-record-list"),
    path("attendance/records/<uuid:pk>", views.RecordDetail.as_view(), name="attendance-record-detail"),
    path("attendance/corrections", views.CorrectionList.as_view(), name="attendance-correction-list"),
    path(
        "attendance/corrections/<uuid:pk>",
        views.CorrectionDetail.as_view(),
        name="attendance-correction-detail",
    ),
    path(
        "attendance/corrections/<uuid:pk>/approve",
        views.CorrectionApproveView.as_view(),
        name="attendance-correction-approve",
    ),
    path(
        "attendance/corrections/<uuid:pk>/decline",
        views.CorrectionDeclineView.as_view(),
        name="attendance-correction-decline",
    ),
]
