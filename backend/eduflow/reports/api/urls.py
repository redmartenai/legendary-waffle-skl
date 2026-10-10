from django.urls import path

from . import views

urlpatterns = [
    path("reports/attendance", views.AttendanceReport.as_view(), name="report-attendance"),
    path("reports/fee-dues", views.FeeDuesReport.as_view(), name="report-fee-dues"),
    path("reports/exam-results", views.ExamResultsReport.as_view(), name="report-exam-results"),
    path("reports/staff-attendance", views.StaffAttendanceReport.as_view(), name="report-staff-attendance"),
    path("reports/admissions", views.AdmissionsReport.as_view(), name="report-admissions"),
]
