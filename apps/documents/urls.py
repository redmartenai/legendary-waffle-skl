from django.urls import path

from . import views

urlpatterns = [
    path("students/<uuid:student_id>/documents", views.StudentDocumentsView.as_view()),
    path("students/<uuid:student_id>/certificates", views.CertificateRequestView.as_view()),
    path("students/<uuid:student_id>/results/<uuid:exam_id>/report.pdf", views.ReportCardPdfView.as_view()),
    path("fees/payments/<uuid:payment_id>/receipt.pdf", views.ReceiptPdfView.as_view()),
    path("fees/invoices/<uuid:invoice_id>/invoice.pdf", views.InvoicePdfView.as_view()),
    path("documents/<uuid:document_id>/file", views.DocumentFileView.as_view()),
]
