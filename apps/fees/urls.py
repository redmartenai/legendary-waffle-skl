from django.urls import path

from . import views

urlpatterns = [
    path("students/<uuid:student_id>/fees", views.StudentFeesView.as_view()),
    path("fees/invoices/<uuid:invoice_id>/checkout", views.InvoiceCheckoutView.as_view()),
    path("fees/payments/<uuid:payment_id>/confirm", views.PaymentConfirmView.as_view()),
    path("fees/webhooks/razorpay", views.RazorpayWebhookView.as_view()),
]
