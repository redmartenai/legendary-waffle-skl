from django.urls import path

from . import views

urlpatterns = [
    path("announcements", views.AnnouncementListView.as_view()),
    path("announcements/estimate", views.EstimateView.as_view()),
    path("announcements/<uuid:announcement_id>/ack", views.AnnouncementAckView.as_view()),
    path("announcements/<uuid:announcement_id>/file", views.AnnouncementFileView.as_view()),
    path("announcements/<uuid:announcement_id>/read", views.AnnouncementReadView.as_view()),
]
