from django.contrib import admin

from .models import Route, Stop, StudentTransport, TransportException, Trip, TripIncident, Vehicle


class ScopedAdmin(admin.ModelAdmin):
    """Admin runs unscoped (platform staff only); list rows from every school."""

    def get_queryset(self, request):
        return self.model.all_objects.all()


class StopInline(admin.TabularInline):
    model = Stop
    extra = 0
    fields = ("sequence", "name", "lat", "lng", "radius_m", "pickup_offset_min", "drop_offset_min", "is_school")


@admin.register(Vehicle)
class VehicleAdmin(ScopedAdmin):
    list_display = ("label", "registration_no", "school", "gps_device_id", "last_seen_at", "is_active")
    search_fields = ("label", "registration_no", "gps_device_id")


@admin.register(Route)
class RouteAdmin(ScopedAdmin):
    list_display = ("code", "name", "school", "vehicle", "length_m", "is_active")
    inlines = [StopInline]


@admin.register(Trip)
class TripAdmin(ScopedAdmin):
    list_display = ("route", "direction", "service_date", "status", "started_at", "ended_at", "auto_closed")
    list_filter = ("status", "direction", "service_date")


@admin.register(TripIncident)
class TripIncidentAdmin(ScopedAdmin):
    list_display = ("kind", "trip", "at", "resolved_at")
    list_filter = ("kind",)


@admin.register(StudentTransport)
class StudentTransportAdmin(ScopedAdmin):
    list_display = ("student", "route", "pickup_stop", "drop_stop", "is_active")


@admin.register(TransportException)
class TransportExceptionAdmin(ScopedAdmin):
    list_display = ("kind", "route", "status", "occurred_at")
    list_filter = ("kind", "status")
