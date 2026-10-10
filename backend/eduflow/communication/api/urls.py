from django.urls import path

from . import views

urlpatterns = [
    path("announcements", views.AnnouncementList.as_view(), name="announcement-list"),
    path("announcements/pending", views.PendingAnnouncementsView.as_view(), name="announcement-pending"),
    path("announcements/<uuid:pk>", views.AnnouncementDetail.as_view(), name="announcement-detail"),
    path("announcements/<uuid:pk>/acknowledge", views.AcknowledgeView.as_view(), name="announcement-ack"),
    path("announcements/<uuid:pk>/responses", views.RespondView.as_view(), name="announcement-respond"),
    path("announcements/<uuid:pk>/acknowledgements", views.AckStatusView.as_view(), name="announcement-acks"),
    path("threads", views.ThreadList.as_view(), name="thread-list"),
    path("threads/<uuid:pk>", views.ThreadDetail.as_view(), name="thread-detail"),
    path("threads/<uuid:pk>/messages", views.ThreadMessagesView.as_view(), name="thread-messages"),
    path("complaints", views.ComplaintList.as_view(), name="complaint-list"),
    path("complaints/sentiment", views.SentimentView.as_view(), name="complaint-sentiment"),
    path("complaints/<uuid:pk>", views.ComplaintDetail.as_view(), name="complaint-detail"),
]
