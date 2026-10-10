from django.urls import path

from . import views

urlpatterns = [
    path("remarks", views.RemarkList.as_view(), name="remark-list"),
    path("remarks/<uuid:pk>", views.RemarkDetail.as_view(), name="remark-detail"),
    path("incidents", views.IncidentList.as_view(), name="incident-list"),
    path("incidents/<uuid:pk>", views.IncidentDetail.as_view(), name="incident-detail"),
    path("incidents/<uuid:pk>/resolve", views.IncidentResolveView.as_view(), name="incident-resolve"),
]
