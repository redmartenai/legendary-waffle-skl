from django.urls import path

from . import marks, views

urlpatterns = [
    path("students/<uuid:student_id>/results", views.StudentResultsView.as_view()),
    path("students/<uuid:student_id>/exams", views.StudentExamsView.as_view()),
    path("students/<uuid:student_id>/exams/<uuid:exam_id>/datesheet.pdf", views.DatesheetPdfView.as_view()),
    path("students/<uuid:student_id>/exams/<uuid:exam_id>/admit-card.pdf", views.AdmitCardPdfView.as_view()),
    path("exams/prep/<uuid:item_id>/toggle", views.PrepItemToggleView.as_view()),
    path("teacher/marks", marks.TeacherMarkSheetsView.as_view()),
    path("marksheets/<uuid:sheet_id>", marks.MarkSheetView.as_view()),
    path("marksheets/<uuid:sheet_id>/submit", marks.MarkSheetSubmitView.as_view()),
    path("marksheets/<uuid:sheet_id>/corrections", marks.MarkCorrectionView.as_view()),
]
