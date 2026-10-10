from django.urls import path

from . import views

urlpatterns = [
    path("hr/settings", views.SettingsView.as_view(), name="hr-settings"),
    path("staff-attendance", views.StaffDayList.as_view(), name="staff-attendance-list"),
    path("staff-attendance/check-in", views.CheckInView.as_view(), name="staff-check-in"),
    path("staff-attendance/check-out", views.CheckOutView.as_view(), name="staff-check-out"),
    path("leave-types", views.LeaveTypeList.as_view(), name="leave-type-list"),
    path("leave-types/<uuid:pk>", views.LeaveTypeDetail.as_view(), name="leave-type-detail"),
    path("leave-requests", views.LeaveList.as_view(), name="leave-list"),
    path("leave-requests/<uuid:pk>", views.LeaveDetail.as_view(), name="leave-detail"),
    path("leave-requests/<uuid:pk>/cancel", views.LeaveCancelView.as_view(), name="leave-cancel"),
    path("leave-balances", views.LeaveBalanceView.as_view(), name="leave-balances"),
    path("staff/<uuid:pk>/salary", views.SalaryView.as_view(), name="staff-salary"),
    path("payroll-runs", views.RunList.as_view(), name="payroll-run-list"),
    path("payroll-runs/<uuid:pk>", views.RunDetail.as_view(), name="payroll-run-detail"),
    path("payroll-runs/<uuid:pk>/process", views.RunProcessView.as_view(), name="payroll-run-process"),
    path("payroll-runs/<uuid:pk>/paid", views.RunPaidView.as_view(), name="payroll-run-paid"),
    path("payslips", views.PayslipList.as_view(), name="payslip-list"),
    path("payslips/<uuid:pk>", views.PayslipDetail.as_view(), name="payslip-detail"),
    path("job-openings", views.OpeningList.as_view(), name="job-opening-list"),
    path("job-openings/<uuid:pk>", views.OpeningDetail.as_view(), name="job-opening-detail"),
    path("candidates", views.CandidateList.as_view(), name="candidate-list"),
    path("candidates/<uuid:pk>", views.CandidateDetail.as_view(), name="candidate-detail"),
    path("candidates/<uuid:pk>/resume", views.CandidateResumeView.as_view(), name="candidate-resume"),
]
