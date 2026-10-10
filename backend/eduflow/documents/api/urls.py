from django.urls import path

from . import views

urlpatterns = [
    path("documents", views.DocumentList.as_view(), name="document-list"),
    path("documents/<uuid:pk>", views.DocumentDetail.as_view(), name="document-detail"),
    path("documents/<uuid:pk>/download", views.DocumentDownloadView.as_view(), name="document-download"),
]
