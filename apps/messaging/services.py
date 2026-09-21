from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import Department, Membership
from apps.core.utils import school_now
from apps.notifications.models import Category, Notification
from apps.notifications.services import notify
from apps.realtime import client as realtime

from .models import Conversation, ConversationMember, Message
from .policy import office_hours_of, office_hours_open

MAX_MESSAGE_LENGTH = 2000


def message_payload(message: Message, viewer_id=None) -> dict:
    sender = message.sender
    return {
        "id": str(message.id),
        "client_id": message.client_id,
        "conversation_id": str(message.conversation_id),
        "body": "" if message.deleted_at else message.body,
        "deleted": message.deleted_at is not None,
        "created_at": message.created_at.isoformat(),
        "sender": {"id": str(sender.id), "name": sender.full_name, "initials": sender.initials},
        "mine": viewer_id is not None and sender.id == viewer_id,
    }


def get_or_create_direct(family_user, staff_user, student, staff_label: str, family_label: str) -> Conversation:
    existing = (
        Conversation.objects.filter(kind=Conversation.Kind.DIRECT, student=student, members__user=family_user)
        .filter(members__user=staff_user)
        .first()
    )
    if existing:
        return existing
    with transaction.atomic():
        conversation = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=student)
        ConversationMember.objects.create(
            conversation=conversation, user=family_user, side=ConversationMember.Side.FAMILY, label=family_label
        )
        ConversationMember.objects.create(
            conversation=conversation, user=staff_user, side=ConversationMember.Side.STAFF, label=staff_label
        )
    return conversation


def get_or_create_department(family_user, department: str, student, family_label: str) -> Conversation:
    conversation = Conversation.objects.filter(
        kind=Conversation.Kind.DEPARTMENT, department=department, student=student, members__user=family_user
    ).first()
    with transaction.atomic():
        if conversation is None:
            conversation = Conversation.objects.create(
                kind=Conversation.Kind.DEPARTMENT, department=department, student=student
            )
            ConversationMember.objects.create(
                conversation=conversation, user=family_user, side=ConversationMember.Side.FAMILY, label=family_label
            )
        # Keep the team in sync with who currently works in that department.
        team = Membership.objects.filter(department=department, is_active=True).select_related("user")
        present = set(ConversationMember.objects.filter(conversation=conversation).values_list("user_id", flat=True))
        for membership in team:
            if membership.user_id not in present:
                ConversationMember.objects.create(
                    conversation=conversation,
                    user=membership.user,
                    side=ConversationMember.Side.STAFF,
                    label=f"{Department(department).label}",
                )
                present.add(membership.user_id)
    return conversation


def send_message(conversation: Conversation, sender, body: str, client_id: str) -> tuple[Message, bool]:
    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Type a message."})
    if len(body) > MAX_MESSAGE_LENGTH:
        raise ValidationError({"body": f"Keep messages under {MAX_MESSAGE_LENGTH} characters."})
    if not client_id:
        raise ValidationError({"client_id": "Missing client id."})

    with transaction.atomic():
        message, created = Message.objects.select_related("sender").get_or_create(
            conversation=conversation, client_id=client_id, defaults={"sender": sender, "body": body}
        )
        if not created:
            return message, False  # retried send: same message, no duplicate alerts
        conversation.last_message_at = message.created_at
        conversation.last_message_preview = body[:140]
        conversation.save(update_fields=["last_message_at", "last_message_preview", "updated_at"])
        ConversationMember.objects.filter(conversation=conversation, user=sender).update(last_read_at=message.created_at)

        members = list(ConversationMember.objects.filter(conversation=conversation).select_related("user"))
        sender_label = next((m.label for m in members if m.user_id == sender.id), "")
        now_local = school_now(conversation.school)
        child = conversation.student.first_name if conversation.student_id else None
        for member in members:
            realtime.publish(
                realtime.personal_channel(member.user_id),
                {"type": "chat.message", "message": message_payload(message, viewer_id=member.user_id)},
            )
            if member.user_id == sender.id or member.muted:
                continue
            # Staff only get pushed during their office hours; the message still waits in their inbox.
            push = True
            if member.side == ConversationMember.Side.STAFF:
                push = office_hours_open(office_hours_of(member.user), now_local)
            title = sender.full_name if member.side == ConversationMember.Side.FAMILY else f"{sender.full_name}"
            if child and member.side == ConversationMember.Side.STAFF:
                title = f"{sender.full_name} · about {child}"
            notify(
                [member.user],
                school=conversation.school,
                category=Category.CHAT,
                title=title,
                body=body[:180],
                data={"conversation_id": str(conversation.id), "type": "chat", "sender_label": sender_label},
                dedupe_key=f"chat:{message.id}",
                push=push,
            )
    return message, True


def mark_read(conversation: Conversation, user) -> None:
    now = timezone.now()
    ConversationMember.objects.filter(conversation=conversation, user=user).update(last_read_at=now)
    Notification.objects.filter(
        user=user, category=Category.CHAT, read_at__isnull=True, data__conversation_id=str(conversation.id)
    ).update(read_at=now)
