from django.urls import path

from . import views

urlpatterns = [
    path("alumni", views.AlumnusList.as_view(), name="alumnus-list"),
    path("alumni/<uuid:pk>", views.AlumnusDetail.as_view(), name="alumnus-detail"),
    path("alumni-events", views.EventList.as_view(), name="alumni-event-list"),
    path("alumni-events/<uuid:pk>", views.EventDetail.as_view(), name="alumni-event-detail"),
    path(
        "alumni-events/<uuid:pk>/registrations",
        views.EventRegistrationsView.as_view(),
        name="alumni-event-registrations",
    ),
    path("alumni-campaigns", views.CampaignList.as_view(), name="campaign-list"),
    path("alumni-campaigns/<uuid:pk>", views.CampaignDetail.as_view(), name="campaign-detail"),
    path(
        "alumni-campaigns/<uuid:pk>/donations",
        views.CampaignDonationsView.as_view(),
        name="campaign-donations",
    ),
]
