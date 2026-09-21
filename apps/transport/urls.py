from django.urls import path

from . import views

urlpatterns = [
    path("students/<uuid:student_id>/transport", views.StudentTransportView.as_view()),
    path("students/<uuid:student_id>/transport/preferences", views.TransportPreferenceView.as_view()),
    path("students/<uuid:student_id>/transport/absences", views.TransportAbsenceView.as_view()),
    path("transport/routes/<uuid:route_id>", views.RouteDetailView.as_view()),
    path("transport/trips/<uuid:trip_id>/live", views.TripLiveView.as_view()),
    path("transport/trips/<uuid:trip_id>/subscription", views.TripSubscriptionView.as_view()),
    path("transport/dashboard", views.TransportDashboardView.as_view()),
    path("transport/ingest/traccar", views.TraccarIngestView.as_view()),
    path("driver/trips", views.DriverTripsView.as_view()),
    path("driver/trips/<uuid:trip_id>", views.DriverTripDetailView.as_view()),
    path("driver/trips/<uuid:trip_id>/start", views.DriverTripStartView.as_view()),
    path("driver/trips/<uuid:trip_id>/end", views.DriverTripEndView.as_view()),
    path("driver/trips/<uuid:trip_id>/positions", views.DriverPositionsView.as_view()),
    path("driver/trips/<uuid:trip_id>/roster", views.DriverRosterView.as_view()),
    path("driver/trips/<uuid:trip_id>/boarding", views.DriverBoardingView.as_view()),
    path("driver/trips/<uuid:trip_id>/sos", views.DriverSosView.as_view()),
]
