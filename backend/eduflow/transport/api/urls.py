from django.urls import path

from . import views

urlpatterns = [
    path("transport/vehicles", views.VehicleList.as_view(), name="vehicle-list"),
    path("transport/vehicles/<uuid:pk>", views.VehicleDetail.as_view(), name="vehicle-detail"),
    path("transport/routes", views.RouteList.as_view(), name="route-list"),
    path("transport/routes/<uuid:pk>", views.RouteDetail.as_view(), name="route-detail"),
    path("transport/routes/<uuid:pk>/trips", views.RouteStartTripView.as_view(), name="route-start-trip"),
    path("transport/riders", views.RiderList.as_view(), name="rider-list"),
    path("transport/riders/<uuid:pk>", views.RiderDetail.as_view(), name="rider-detail"),
    path("transport/trips", views.TripList.as_view(), name="trip-list"),
    path("transport/trips/<uuid:pk>", views.TripDetail.as_view(), name="trip-detail"),
    path("transport/trips/<uuid:pk>/arrive", views.TripArriveView.as_view(), name="trip-arrive"),
    path("transport/trips/<uuid:pk>/delay", views.TripDelayView.as_view(), name="trip-delay"),
    path("transport/trips/<uuid:pk>/positions", views.TripPositionView.as_view(), name="trip-positions"),
    path("transport/maintenance", views.MaintenanceList.as_view(), name="maintenance-list"),
    path("transport/maintenance/<uuid:pk>", views.MaintenanceDetail.as_view(), name="maintenance-detail"),
]
