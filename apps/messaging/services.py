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


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


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
        "attachment": {
            "name": message.attachment_name or message.attachment.name.rsplit("/", 1)[-1],
            "size": message.attachment_size,
            "download": f"/chat/messages/{message.id}/file",
        }
        if message.attachment and not message.deleted_at
        else None,
    }


def meeting_payload(meeting) -> dict:
    return {
        "id": str(meeting.id),
        "title": meeting.title,
        "starts_at": meeting.starts_at.isoformat(),
        "ends_at": meeting.ends_at.isoformat(),
        "location": meeting.location,
        "status": meeting.status,
        "created_at": meeting.created_at.isoformat(),
        "calendar": f"/meetings/{meeting.id}.ics",
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


def get_or_create_colleague(user, other, user_label: str, other_label: str) -> Conversation:
    """A staff-to-staff chat (no child attached)."""
    existing = (
        Conversation.objects.filter(kind=Conversation.Kind.DIRECT, student__isnull=True, members__user=user)
        .filter(members__user=other)
        .first()
    )
    if existing:
        return existing
    with transaction.atomic():
        conversation = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=None)
        ConversationMember.objects.create(conversation=conversation, user=user, side=ConversationMember.Side.STAFF, label=user_label)
        ConversationMember.objects.create(conversation=conversation, user=other, side=ConversationMember.Side.STAFF, label=other_label)
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


def send_message(conversation: Conversation, sender, body: str, client_id: str, attachment=None) -> tuple[Message, bool]:
    body = (body or "").strip()
    if not body and attachment is None:
        raise ValidationError({"body": "Type a message."})
    if attachment is not None and attachment.size > MAX_ATTACHMENT_BYTES:
        raise ValidationError({"attachment": "Files can be up to 10 MB."})
    if len(body) > MAX_MESSAGE_LENGTH:
        raise ValidationError({"body": f"Keep messages under {MAX_MESSAGE_LENGTH} characters."})
    if not client_id:
        raise ValidationError({"client_id": "Missing client id."})

    with transaction.atomic():
        defaults = {"sender": sender, "body": body}
        if attachment is not None:
            defaults.update(attachment=attachment, attachment_name=attachment.name[:120], attachment_size=attachment.size)
        message, created = Message.objects.select_related("sender").get_or_create(
            conversation=conversation, client_id=client_id, defaults=defaults
        )
        if not created:
            return message, False  # retried send: same message, no duplicate alerts
        conversation.last_message_at = message.created_at
        conversation.last_message_preview = (body or f"📎 {message.attachment_name}")[:140]
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


def decide_meeting(meeting, user, action: str, starts_at=None):
    """Staff answer a requested meeting: accept the parent's slot, or propose another time. The thread shows it."""
    from datetime import datetime

    from .models import Meeting

    if meeting.status != Meeting.Status.REQUESTED:
        raise ValidationError({"status": "This request has already been answered."})
    if action == "accept":
        meeting.status = Meeting.Status.BOOKED
        meeting.booked_by = user
        meeting.save(update_fields=["status", "booked_by", "updated_at"])
        text = f"Confirmed: {meeting.title}, {timezone.localtime(meeting.starts_at):%a %d %b, %I:%M %p}".replace(" 0", " ")
    elif action == "propose":
        try:
            starts = starts_at if isinstance(starts_at, datetime) else datetime.fromisoformat(str(starts_at))
        except ValueError as exc:
            raise ValidationError({"starts_at": "Pick a time."}) from exc
        if timezone.is_naive(starts):
            starts = timezone.make_aware(starts)
        if starts <= timezone.now():
            raise ValidationError({"starts_at": "Pick a time in the future."})
        length = meeting.ends_at - meeting.starts_at
        meeting.starts_at, meeting.ends_at = starts, starts + length
        meeting.save(update_fields=["starts_at", "ends_at", "updated_at"])
        text = f"Could we meet at {timezone.localtime(starts):%a %d %b, %I:%M %p} instead?".replace(" 0", " ")
    else:
        raise ValidationError({"action": "Accept or propose a time."})
    send_message(meeting.conversation, user, text, client_id=f"meeting:{meeting.id}:{action}:{timezone.now().timestamp():.0f}")
    return meeting
