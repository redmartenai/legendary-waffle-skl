from django.urls import path

from . import views

urlpatterns = [path("audit-events", views.AuditEventListView.as_view(), name="audit-event-list")]
