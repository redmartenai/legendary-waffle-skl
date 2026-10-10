from django.urls import path

from . import views

urlpatterns = [
    path("hostels", views.HostelList.as_view(), name="hostel-list"),
    path("hostels/<uuid:pk>", views.HostelDetail.as_view(), name="hostel-detail"),
    path("hostels/<uuid:pk>/rooms", views.HostelRoomsView.as_view(), name="hostel-rooms"),
    path("hostels/<uuid:pk>/roll-call", views.HostelRollView.as_view(), name="hostel-roll-call"),
    path("hostel/allocations", views.AllocationList.as_view(), name="hostel-allocation-list"),
    path("hostel/allocations/<uuid:pk>", views.AllocationDetail.as_view(), name="hostel-allocation-detail"),
    path("hostel/outpasses", views.OutpassList.as_view(), name="outpass-list"),
    path("hostel/outpasses/<uuid:pk>", views.OutpassDetail.as_view(), name="outpass-detail"),
    path("hostel/outpasses/<uuid:pk>/gate", views.OutpassGateView.as_view(), name="outpass-gate"),
    path("hostel/outpasses/<uuid:pk>/cancel", views.OutpassCancelView.as_view(), name="outpass-cancel"),
    path("hostel/roll-call", views.RollList.as_view(), name="hostel-roll-list"),
]
