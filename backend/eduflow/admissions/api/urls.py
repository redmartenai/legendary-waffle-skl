from django.urls import path

from . import views

urlpatterns = [
    path("admissions/apply", views.OnlineApplicationView.as_view(), name="admission-apply"),
    path("admissions", views.ApplicationList.as_view(), name="admission-list"),
    path("admissions/<uuid:pk>", views.ApplicationDetail.as_view(), name="admission-detail"),
    path("admissions/<uuid:pk>/move", views.ApplicationMoveView.as_view(), name="admission-move"),
    path("admissions/<uuid:pk>/enrol", views.ApplicationEnrolView.as_view(), name="admission-enrol"),
    path(
        "admissions/<uuid:pk>/documents",
        views.ApplicationDocumentListView.as_view(),
        name="admission-documents",
    ),
    path(
        "admissions/<uuid:pk>/documents/<uuid:document_id>",
        views.ApplicationDocumentDownloadView.as_view(),
        name="admission-document-download",
    ),
]
