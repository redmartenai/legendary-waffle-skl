from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Membership, OtpChallenge, PushDevice, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("full_name",)
    list_display = ("full_name", "phone", "language", "is_staff", "is_active")
    search_fields = ("full_name", "phone", "email")
    fieldsets = (
        (None, {"fields": ("phone", "password")}),
        ("Profile", {"fields": ("full_name", "email", "language", "quiet_hours_start", "quiet_hours_end")}),
        ("Platform access", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("phone", "full_name", "password1", "password2")}),)
    filter_horizontal = ("groups", "user_permissions")


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "school", "role", "title", "department", "is_active")
    list_filter = ("role", "is_active", "school")
    search_fields = ("user__full_name", "user__phone")
    raw_id_fields = ("user",)

    def get_queryset(self, request):
        return Membership.all_objects.select_related("user", "school")


@admin.register(PushDevice)
class PushDeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "platform", "app_variant", "is_active", "last_seen_at")


@admin.register(OtpChallenge)
class OtpChallengeAdmin(admin.ModelAdmin):
    list_display = ("phone", "school", "created_at", "expires_at", "attempts", "consumed_at")
    readonly_fields = ("code_hash",)
