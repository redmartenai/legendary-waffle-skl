from django.urls import path

from . import views

urlpatterns = [
    path("announcements", views.AnnouncementListView.as_view()),
    path("announcements/<uuid:announcement_id>/ack", views.AnnouncementAckView.as_view()),
]
