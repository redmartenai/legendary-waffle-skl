from django.db.models import Count, F, OuterRef, Q, Subquery
from django.http import Http404
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.academics.models import Student
from apps.accounts.models import Department, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import initials, school_now

from . import policy, services
from .models import Conversation, ConversationMember, Message


def _member_or_404(request, conversation_id) -> tuple[Conversation, ConversationMember]:
    member = (
        ConversationMember.objects.select_related("conversation", "conversation__student")
        .filter(conversation_id=conversation_id, user=request.user)
        .first()
    )
    if member is None:
        raise Http404
    return member.conversation, member


def _conversation_payload(request, conversation, member, others, unread: int, last: Message | None) -> dict:
    staff_side = [m for m in others if m.side == ConversationMember.Side.STAFF]
    if conversation.kind == Conversation.Kind.DEPARTMENT:
        title = Department(conversation.department).label if conversation.department else "Office"
        subtitle = "Office team"
        avatar = initials(title)
    elif member.side == ConversationMember.Side.FAMILY and staff_side:
        title, subtitle, avatar = staff_side[0].user.full_name, staff_side[0].label, staff_side[0].user.initials
    elif others:
        title, subtitle, avatar = others[0].user.full_name, others[0].label, others[0].user.initials
    else:
        title, subtitle, avatar = "Conversation", "", "?"

    office_hours = None
    if member.side == ConversationMember.Side.FAMILY and conversation.kind == Conversation.Kind.DIRECT and staff_side:
        hours = policy.office_hours_of(staff_side[0].user)
        if hours:
            office_hours = {
                "text": policy.office_hours_text(hours),
                "open_now": policy.office_hours_open(hours, school_now(request.school)),
            }
    student = conversation.student
    return {
        "id": str(conversation.id),
        "kind": conversation.kind,
        "title": title,
        "subtitle": subtitle,
        "initials": avatar,
        "student": {"id": str(student.id), "name": student.full_name, "first_name": student.first_name}
        if student
        else None,
        "unread": unread,
        "last_message": {
            "body": last.body[:140] if not last.deleted_at else "",
            "at": last.created_at.isoformat(),
            "mine": last.sender_id == request.user.id,
        }
        if last
        else None,
        "office_hours": office_hours,
        "can_send": True,
    }


def _unread_counts(user, conversation_ids) -> dict:
    last_read = ConversationMember.objects.filter(conversation=OuterRef("conversation"), user=user).values(
        "last_read_at"
    )[:1]
    rows = (
        Message.objects.filter(conversation_id__in=conversation_ids, deleted_at__isnull=True)
        .exclude(sender=user)
        .annotate(read_at=Subquery(last_read))
        .filter(Q(read_at__isnull=True) | Q(created_at__gt=F("read_at")))
        .values("conversation_id")
        .annotate(n=Count("id"))
    )
    return {row["conversation_id"]: row["n"] for row in rows}


class ChatContactsView(SchoolAPIView):
    def get(self, request):
        return Response({"contacts": policy.family_contacts(request)})


class ConversationListView(SchoolAPIView):
    def get(self, request):
        memberships = list(
            ConversationMember.objects.filter(user=request.user)
            .select_related("conversation", "conversation__student")
            .order_by("-conversation__last_message_at", "-conversation__created_at")[:100]
        )
        ids = [m.conversation_id for m in memberships]
        others: dict = {}
        for other in ConversationMember.objects.filter(conversation_id__in=ids).exclude(user=request.user).select_related("user"):
            others.setdefault(other.conversation_id, []).append(other)
        lasts: dict = {}
        for message in Message.objects.filter(conversation_id__in=ids).order_by("conversation_id", "-created_at"):
            lasts.setdefault(message.conversation_id, message)
        unread = _unread_counts(request.user, ids)
        items = [
            _conversation_payload(
                request, m.conversation, m, others.get(m.conversation_id, []), unread.get(m.conversation_id, 0), lasts.get(m.conversation_id)
            )
            for m in memberships
        ]
        return Response({"conversations": items, "unread_total": sum(unread.values())})

    def post(self, request):
        """Start (or reopen) a conversation about a child."""
        student = Student.objects.filter(id=request.data.get("student_id"), is_active=True).select_related("class_group").first()
        if student is None:
            raise ValidationError({"student_id": "Choose which child this is about."})
        kind = request.data.get("kind")
        family_roles = request.roles & {Role.PARENT, Role.STUDENT}

        if kind == "department":
            department = request.data.get("department")
            if department not in Department.values:
                raise ValidationError({"department": "Unknown team."})
            if not family_roles or not any(
                c["kind"] == "department" and c["department"] == department and c["student"]["id"] == str(student.id)
                for c in policy.family_contacts(request)
            ):
                raise PermissionDenied("You can't message this team about this child.")
            conversation = services.get_or_create_department(
                request.user, department, student, family_label=f"Parent of {student.first_name}"
            )
        elif kind == "user":
            target_id = request.data.get("user_id")
            if family_roles and policy.family_may_message_teacher(request, target_id, student):
                contact = next(
                    c for c in policy.family_contacts(request)
                    if c["kind"] == "user" and c["user_id"] == str(target_id) and c["student"]["id"] == str(student.id)
                )
                from apps.accounts.models import User

                teacher = User.objects.get(id=target_id)
                conversation = services.get_or_create_direct(
                    request.user,
                    teacher,
                    student,
                    staff_label=contact["subtitle"],
                    family_label=f"Parent of {student.first_name}",
                )
            elif policy.staff_may_message_student_family(request, student):
                # Staff start a chat with one of the child's guardians.
                guardians = policy.guardian_users(student)
                target = next((g for g in guardians if str(g.id) == str(target_id)), None)
                if target is None:
                    raise PermissionDenied("That person isn't this child's guardian.")
                label = "Class teacher" if student.class_group.class_teacher_id == request.user.id else "Teacher"
                conversation = services.get_or_create_direct(
                    target,
                    request.user,
                    student,
                    staff_label=f"{label} · {student.class_group.short_label}",
                    family_label=f"Parent of {student.first_name}",
                )
            else:
                raise PermissionDenied("You can't start a chat with this person.")
        else:
            raise ValidationError({"kind": "Use user or department."})

        member = ConversationMember.objects.get(conversation=conversation, user=request.user)
        others = list(ConversationMember.objects.filter(conversation=conversation).exclude(user=request.user).select_related("user"))
        last = Message.objects.filter(conversation=conversation).order_by("-created_at").first()
        payload = _conversation_payload(request, conversation, member, others, 0, last)
        return Response(payload, status=status.HTTP_201_CREATED)


class MessageListView(SchoolAPIView):
    def get(self, request, conversation_id):
        conversation, member = _member_or_404(request, conversation_id)
        queryset = Message.objects.filter(conversation=conversation).select_related("sender").order_by("-created_at")
        before = request.query_params.get("before")
        if before:
            moment = parse_datetime(before)
            if moment is None:
                raise ValidationError({"before": "Use an ISO timestamp."})
            queryset = queryset.filter(created_at__lt=moment)
        page = list(queryset[:40])
        page.reverse()
        return Response(
            {
                "messages": [services.message_payload(m, viewer_id=request.user.id) for m in page],
                "has_more": len(page) == 40,
            }
        )

    def post(self, request, conversation_id):
        conversation, _ = _member_or_404(request, conversation_id)
        message, created = services.send_message(
            conversation, request.user, request.data.get("body", ""), str(request.data.get("client_id", ""))[:64]
        )
        return Response(
            services.message_payload(message, viewer_id=request.user.id),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ConversationReadView(SchoolAPIView):
    def post(self, request, conversation_id):
        conversation, _ = _member_or_404(request, conversation_id)
        services.mark_read(conversation, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
