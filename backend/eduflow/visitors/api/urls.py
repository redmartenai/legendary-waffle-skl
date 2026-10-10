from django.urls import path

from . import views

urlpatterns = [
    path("visits", views.VisitList.as_view(), name="visit-list"),
    path("visits/scan", views.VisitScanView.as_view(), name="visit-scan"),
    path("visits/inside", views.VisitInsideView.as_view(), name="visit-inside"),
    path("visits/<uuid:pk>", views.VisitDetail.as_view(), name="visit-detail"),
    path("visits/<uuid:pk>/decision", views.VisitDecisionView.as_view(), name="visit-decision"),
    path("visits/<uuid:pk>/pass", views.VisitPassView.as_view(), name="visit-pass"),
    path("visits/<uuid:pk>/cancel", views.VisitCancelView.as_view(), name="visit-cancel"),
]
