from django.contrib import admin

from .models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("sender", "body", "created_at", "deleted_at")
    readonly_fields = ("sender", "body", "created_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    """Read-only review for safeguarding cases (platform staff)."""

    list_display = ("id", "kind", "school", "student", "department", "last_message_at")
    list_filter = ("kind",)
    inlines = [MessageInline]

    def get_queryset(self, request):
        return Conversation.all_objects.select_related("student", "school")
