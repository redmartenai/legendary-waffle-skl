"""Who an announcement reaches, and sending it on each channel."""

from django.utils import timezone

from apps.academics.models import ClassGroup, Student, StudentGuardian
from apps.accounts.models import STAFF_ROLES, Membership, PushDevice, Role
from apps.notifications.channels import address_for, provider_for
from apps.notifications.models import Category, Priority
from apps.notifications.services import notify

from .models import Announcement, ChannelDelivery

CHANNELS = ("push", "in_app", "sms", "whatsapp", "email")
SMS_LIMIT = 160


def resolve(audience: str, groups, route=None, parents_only: bool = False) -> dict:
    """Students in scope, the family users (guardians + student logins) and staff users an audience reaches.

    ``parents_only`` leaves out the students' own logins (a class audience "· Parents").
    """
    students = Student.objects.filter(is_active=True)
    if audience in (Announcement.Audience.CLASSES,):
        students = students.filter(class_group__in=groups)
    elif audience == Announcement.Audience.ROUTE:
        from apps.transport.models import StudentTransport

        students = students.filter(id__in=StudentTransport.objects.filter(route=route, is_active=True).values("student_id"))
    elif audience == Announcement.Audience.STAFF:
        students = students.none()
    student_list = list(students.select_related("user"))
    family = {link.user for link in StudentGuardian.objects.filter(student__in=student_list).select_related("user") if link.user.is_active}
    guardians = set(family)
    if audience != Announcement.Audience.PARENTS and not parents_only:
        family |= {s.user for s in student_list if s.user_id}
    staff = set()
    if audience in (Announcement.Audience.EVERYONE, Announcement.Audience.STAFF):
        staff = {m.user for m in Membership.objects.filter(role__in=STAFF_ROLES, is_active=True).select_related("user")}
    teachers = set(Membership.objects.filter(role=Role.TEACHER, is_active=True).values_list("user_id", flat=True))
    return {"students": student_list, "family": family, "guardians": guardians, "staff": staff, "teacher_ids": teachers}


def estimate(audience: str, groups, route=None, parents_only: bool = False) -> dict:
    r = resolve(audience, groups, route, parents_only)
    users = r["family"] | r["staff"]
    with_app = set(PushDevice.objects.filter(user__in=users, is_active=True).values_list("user_id", flat=True))
    # A family counts as "on the app" if any of its guardians is.
    guardians_by_student: dict = {}
    for link in StudentGuardian.objects.filter(student__in=r["students"]).values("student_id", "user_id"):
        guardians_by_student.setdefault(link["student_id"], set()).add(link["user_id"])
    families = len(r["students"])
    families_on_app = sum(1 for g in guardians_by_student.values() if g & with_app)
    staff_teachers = sum(1 for u in r["staff"] if u.id in r["teacher_ids"])
    return {
        "families": families,
        "parents": len(r["guardians"]),
        "people": len(users),
        "sections": len({s.class_group_id for s in r["students"]}),
        "staff": len(r["staff"]),
        "teachers": staff_teachers,
        "support": len(r["staff"]) - staff_teachers,
        "push": sum(1 for u in users if u.id in with_app),
        "families_on_app": families_on_app,
        "families_without_app": families - families_on_app,
        "sms": sum(1 for u in users if u.id not in with_app and u.phone),
        "whatsapp": sum(1 for u in users if u.phone and ((u.preferences or {}).get("channels") or {}).get("whatsapp")),
        "email": sum(1 for u in users if u.email),
    }


# Merge fields the composer's "Insert field" offers. Per-person fields are filled on SMS, WhatsApp and email;
# push and the in-app inbox get one text for everyone, so they use the generic value.
FIELDS = ("parent_name", "student_name", "class", "school_name", "principal_name")


def render_fields(text: str, item: Announcement, user=None, student=None) -> str:
    school = item.school
    values = {
        "parent_name": (user.full_name if user else "") or "Parent",
        "student_name": student.first_name if student else "your child",
        "class": student.class_group.short_label if student else "your child's class",
        "school_name": school.name,
        "principal_name": item.created_by.full_name if item.created_by else school.name,
    }
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def plain(text: str) -> str:
    """The composer's light formatting (**bold**, _italic_, [label](url)) as plain text for SMS and push."""
    import re

    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    return re.sub(r"\[([^\]]+)\]\((\S+?)\)", r"\1 (\2)", text)


def sms_text(item: Announcement, link: str, body: str | None = None) -> str:
    text = f"{item.title}: {plain(body if body is not None else item.body)}".replace("\n", " ")
    room = SMS_LIMIT - len(link) - 2
    return (text[: room - 1] + "…" if len(text) > room else text) + " " + link


def deliver(item: Announcement, *, exclude=None) -> dict:
    """Send an announcement on its channels. Idempotent: an announcement is delivered once."""
    if item.delivered_at or item.status == Announcement.Status.DRAFT:
        return {}
    r = resolve(item.audience, list(item.class_groups.all()), item.route, item.parents_only)
    users = (r["family"] | r["staff"]) - ({exclude} if exclude else set())
    # The first child in scope, for the per-person fields.
    child_of: dict = {}
    for link in StudentGuardian.objects.filter(student__in=r["students"], user__in=users).select_related("student__class_group"):
        child_of.setdefault(link.user_id, link.student)
    generic = " ".join(plain(render_fields(item.body, item)).split())
    channels = set(item.channels or ["push", "in_app"])
    counts = {}
    if channels & {"push", "in_app"}:
        notify(
            users,
            school=item.school,
            category=Category.SAFETY if item.kind == Announcement.Kind.SAFETY else Category.ANNOUNCEMENT,
            title=item.title,
            body=generic[:180],
            data={"announcement_id": str(item.id), "type": "announcement"},
            dedupe_key=f"announcement:{item.id}",
            priority=Priority.CRITICAL if item.kind == Announcement.Kind.SAFETY else Priority.NORMAL,
            push="push" in channels,
        )
        counts["push" if "push" in channels else "in_app"] = len(users)
    with_app = set(PushDevice.objects.filter(user__in=users, is_active=True).values_list("user_id", flat=True))
    link = f"eduflow.app/a/{str(item.id)[:8]}"
    for channel in ("sms", "whatsapp", "email"):
        if channel not in channels:
            continue
        provider = provider_for(channel)
        sent = 0
        rows = []
        for user in users:
            # SMS is for families without the app; WhatsApp only to people who opted in.
            if channel == "sms" and user.id in with_app:
                continue
            if channel == "whatsapp" and not ((user.preferences or {}).get("channels") or {}).get("whatsapp"):
                continue
            address = address_for(channel, user)
            if not address:
                continue
            body = render_fields(item.body, item, user, child_of.get(user.id))
            text = sms_text(item, link, body) if channel == "sms" else f"{item.title}\n\n{plain(body)}"
            ref = provider.send(channel, address, text)
            rows.append(ChannelDelivery(school=item.school, announcement=item, channel=channel, user=user, address=address, provider=provider.name, status=getattr(provider, "delivered_status", "sent"), provider_ref=ref))
            sent += 1
        ChannelDelivery.objects.bulk_create(rows)
        counts[channel] = sent
    item.delivered_at = timezone.now()
    item.recipients = len(users)
    item.save(update_fields=["delivered_at", "recipients", "updated_at"])
    return counts


def deliver_due(now=None) -> int:
    now = now or timezone.now()
    # Only broadcasts made with channels are queued; older notices (and seeded ones) were sent when posted.
    due = Announcement.objects.filter(delivered_at__isnull=True, published_at__lte=now, status=Announcement.Status.PUBLISHED).exclude(channels=[])
    n = 0
    for item in due:
        deliver(item, exclude=item.created_by)
        n += 1
    return n


def groups_for(grades: list[str], class_ids: list) -> list:
    qs = ClassGroup.objects.none()
    if grades:
        qs = ClassGroup.objects.filter(grade__in=grades)
    if class_ids:
        qs = qs | ClassGroup.objects.filter(id__in=class_ids)
    return list(qs.distinct())
