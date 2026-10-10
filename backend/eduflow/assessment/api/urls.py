from django.urls import path

from . import views

urlpatterns = [
    path("exams", views.ExamList.as_view(), name="exam-list"),
    path("exams/<uuid:pk>", views.ExamDetail.as_view(), name="exam-detail"),
    path("exams/<uuid:pk>/sheets", views.ExamSheetsView.as_view(), name="exam-sheets"),
    path("exams/<uuid:pk>/sheets/generate", views.ExamGenerateView.as_view(), name="exam-generate"),
    path("exams/<uuid:pk>/publish", views.ExamPublishView.as_view(), name="exam-publish"),
    path("exams/<uuid:pk>/results", views.ExamResultsView.as_view(), name="exam-results"),
    path("mark-sheets/<uuid:pk>", views.MarkSheetDetailView.as_view(), name="mark-sheet-detail"),
    path("mark-sheets/<uuid:pk>/marks", views.MarkSheetMarksView.as_view(), name="mark-sheet-marks"),
    path("mark-sheets/<uuid:pk>/submit", views.MarkSheetSubmitView.as_view(), name="mark-sheet-submit"),
    path("marks/<uuid:pk>/corrections", views.MarkCorrectionView.as_view(), name="mark-correction"),
    path("students/<uuid:pk>/report-card", views.ReportCardView.as_view(), name="report-card"),
    path("grade-bands", views.GradeBandList.as_view(), name="grade-band-list"),
    path("grade-bands/<uuid:pk>", views.GradeBandDetail.as_view(), name="grade-band-detail"),
]
