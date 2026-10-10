from django.urls import path

from . import views

urlpatterns = [
    path("fee-plans", views.PlanList.as_view(), name="fee-plan-list"),
    path("fee-plans/<uuid:pk>", views.PlanDetail.as_view(), name="fee-plan-detail"),
    path("fee-plans/<uuid:pk>/assign", views.PlanAssignView.as_view(), name="fee-plan-assign"),
    path("student-fees", views.StudentFeeList.as_view(), name="student-fee-list"),
    path("student-fees/<uuid:pk>", views.StudentFeeDetail.as_view(), name="student-fee-detail"),
    path("fee-payments", views.PaymentList.as_view(), name="fee-payment-list"),
    path("fee-payments/<uuid:pk>", views.PaymentDetail.as_view(), name="fee-payment-detail"),
    path("fee-payments/<uuid:pk>/refunds", views.RefundRequestView.as_view(), name="fee-refunds"),
    path("students/<uuid:pk>/fees", views.StudentStatementView.as_view(), name="student-fee-statement"),
    path("fees/defaulters", views.DefaultersView.as_view(), name="fee-defaulters"),
    path("fees/collections", views.CollectionsView.as_view(), name="fee-collections"),
]
