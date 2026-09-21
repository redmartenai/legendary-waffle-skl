from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "category", "priority", "push_status", "created_at", "read_at")
    list_filter = ("category", "priority", "push_status")
    search_fields = ("title", "user__full_name", "dedupe_key")
    raw_id_fields = ("user",)
