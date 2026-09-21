from django.contrib import admin

from .models import Organization, School


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "kind", "city", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "code", "city")
