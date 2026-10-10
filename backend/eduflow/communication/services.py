"""Communication writes and selectors. Transactional and audited (``communication.*``). Message and complaint
texts are never copied into audit metadata."""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.db.models import Avg, Count, Q, QuerySet
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import Section, Subject
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people import policies as people_policies
from eduflow.people.models import Guardian, StaffProfile, Student, TeacherAssignment
from eduflow.people.scoping import may_write
from eduflow.tenancy import domain
from eduflow.tenancy.models import Membership

from .models import (
    Acknowledgement,
    Announcement,
    AnnouncementResponse,
    Audience,
    Complaint,
    ComplaintStatus,
    Message,
    Thread,
    ThreadKind,
)

REPLY_HOURS = 24  # prototype RULES.replyHours


# ---------------------------------------------------------------------------------------------- announcements
def recipients(announcement: Announcement) -> QuerySet[Membership]:
    """Active members an announcement is addressed to."""
    school_id = announcement.school_id
    section = announcement.section_id
    q = Q(pk__in=[])
    if Audience.STAFF in announcement.audiences:
        staff = StaffProfile.objects.filter(school_id=school_id)
        if section:
            staff = staff.filter(assignments__section_id=section, assignments__status="active")
        q |= Q(pk__in=staff.values("membership_id"))
    if Audience.PARENTS in announcement.audiences:
        guardians = Guardian.objects.filter(school_id=school_id, membership__isnull=False)
        if section:
            guardians = guardians.filter(
                student_links__student__enrollments__section_id=section,
                student_links__student__enrollments__status="active",
            )
        q |= Q(pk__in=guardians.values("membership_id"))
    if Audience.STUDENTS in announcement.audiences:
        students = Student.objects.filter(school_id=school_id, membership__isnull=False)
        if section:
            students = students.filter(enrollments__section_id=section, enrollments__status="active")
        q |= Q(pk__in=students.values("membership_id"))
    return Membership.objects.filter(school_id=school_id, is_active=True).filter(q).distinct()


@transaction.atomic
def publish(
    actor: Actor,
    *,
    title: str,
    body: str,
    audiences: list[str],
    section_id: Any = None,
    pinned: bool = False,
    requires_acknowledgement: bool = True,
) -> Announcement:
    section = (
        domain.resolve(Section, actor.school, section_id, "section_id", label="section")
        if section_id
        else None
    )
    school_wide = DataScope.SCHOOL in actor.scopes("announcement.manage")
    if not school_wide and (section is None or not may_write(actor, "announcement.manage", section)):
        raise PermissionDenied("You can announce only to a section you teach.")
    announcement = Announcement.objects.create(
        school=actor.school,
        title=title,
        body=body,
        audiences=sorted(set(audiences)),
        section=section,
        pinned=pinned,
        requires_acknowledgement=requires_acknowledgement,
        author=actor.membership,
    )
    domain.record(
        "communication.announcement.published",
        announcement,
        audiences=announcement.audiences,
        section=str(section.pk) if section else None,
    )
    notifications.notify(
        actor.school,
        [m for m in recipients(announcement) if m.pk != actor.membership.pk],
        kind="announcement",
        title=title,
        body=body[:200],
        link=("announcement", announcement.pk),
    )
    return announcement


def check_author(actor: Actor, announcement: Announcement) -> None:
    if DataScope.SCHOOL in actor.scopes("announcement.manage"):
        return
    if announcement.author_id != actor.membership.pk:
        raise PermissionDenied("Only the author or the office can change this announcement.")


@transaction.atomic
def update(actor: Actor, announcement: Announcement, **data: Any) -> Announcement:
    check_author(actor, announcement)
    changed = domain.apply_changes(announcement, data)
    if changed:
        announcement.save()
        domain.record("communication.announcement.updated", announcement, fields=changed)
    return announcement


@transaction.atomic
def archive(actor: Actor, announcement: Announcement) -> None:
    check_author(actor, announcement)
    if announcement.archived_at is None:
        announcement.archived_at = timezone.now()
        announcement.save(update_fields=["archived_at"])
        domain.record("communication.announcement.archived", announcement)


def _addressed(actor: Actor, announcement: Announcement) -> None:
    if not recipients(announcement).filter(pk=actor.membership.pk).exists():
        raise PermissionDenied("This announcement is not addressed to you.")


@transaction.atomic
def acknowledge(actor: Actor, announcement: Announcement) -> Acknowledgement:
    _addressed(actor, announcement)
    ack, created = Acknowledgement.objects.get_or_create(
        school_id=actor.school.pk, announcement=announcement, member=actor.membership
    )
    if created:
        domain.record("communication.announcement.acknowledged", announcement)
    return ack


@transaction.atomic
def respond(actor: Actor, announcement: Announcement, *, text: str) -> AnnouncementResponse:
    _addressed(actor, announcement)
    if not StaffProfile.objects.filter(membership=actor.membership).exists():
        raise PermissionDenied("Only staff respond to announcements.")
    response = AnnouncementResponse.objects.create(
        school=actor.school, announcement=announcement, member=actor.membership, text=text
    )
    domain.record("communication.announcement.responded", announcement)
    return response


def acknowledgement_status(announcement: Announcement) -> dict[str, Any]:
    everyone = list(recipients(announcement).select_related("user"))
    acked = set(announcement.acknowledgements.values_list("member_id", flat=True))
    return {
        "recipients": len(everyone),
        "acknowledged": len([m for m in everyone if m.pk in acked]),
        "pending": [m for m in everyone if m.pk not in acked],
    }


def pending_for(actor: Actor) -> list[Announcement]:
    """Announcements addressed to the caller that need an acknowledgement they have not given."""
    from . import policies

    visible = policies.announcements.queryset(actor, "announcement.read").filter(
        requires_acknowledgement=True, archived_at__isnull=True
    )
    return [
        a
        for a in visible.exclude(acknowledgements__member=actor.membership).order_by("-pinned", "-created_at")
        if recipients(a).filter(pk=actor.membership.pk).exists()
    ]


# ------------------------------------------------------------------------------------------------ threads
def _teaches(staff: StaffProfile, student: Student) -> QuerySet[TeacherAssignment]:
    return TeacherAssignment.objects.filter(
        staff=staff,
        status="active",
        section__enrollments__student=student,
        section__enrollments__status="active",
    )


@transaction.atomic
def open_thread(
    actor: Actor, *, student_id: Any, staff_id: Any = None, guardian_id: Any = None, subject_id: Any = None
) -> Thread:
    """Open (or return) the conversation about a student between a teacher and a guardian or the student."""
    me = actor.membership
    my_staff = StaffProfile.objects.filter(membership=me).first()
    my_guardian = Guardian.objects.filter(membership=me).first()
    student = people_policies.students.queryset(actor, "message.send").filter(pk=student_id).first()
    if student is None:
        raise ValidationError({"student_id": ["Unknown student."]})
    if my_staff is not None and staff_id in (None, my_staff.pk):
        staff = my_staff
        guardian = None
        if guardian_id:
            guardian = Guardian.objects.filter(pk=guardian_id, student_links__student=student).first()
            if guardian is None:
                raise ValidationError({"guardian_id": ["This guardian is not linked to the student."]})
    elif my_guardian is not None or student.membership_id == me.pk:
        found = StaffProfile.objects.filter(school_id=actor.school.pk, pk=staff_id).first()
        if found is None:
            raise ValidationError({"staff_id": ["Unknown teacher."]})
        staff = found
        guardian = my_guardian if student.membership_id != me.pk else None
    else:
        raise PermissionDenied("Only teachers, guardians and students take part in threads.")
    assignments = _teaches(staff, student)
    if not assignments.exists():
        raise ValidationError({"staff_id": ["This teacher does not teach the student."]})
    subject = domain.resolve(Subject, actor.school, subject_id, "subject_id") if subject_id else None
    if guardian is None:
        if student.membership_id is None:
            raise ValidationError({"guardian_id": ["The student does not sign in; choose a guardian."]})
        kind = ThreadKind.STUDENT_TEACHER
    elif assignments.filter(is_class_teacher=True).exists() and subject is None:
        kind = ThreadKind.PARENT_CLASS_TEACHER
    else:
        kind = ThreadKind.PARENT_SUBJECT_TEACHER
        if subject is None:
            first = assignments.exclude(subject__isnull=True).select_related("subject").first()
            subject = first.subject if first else None
    thread, created = Thread.objects.get_or_create(
        school_id=actor.school.pk,
        kind=kind,
        student=student,
        staff=staff,
        guardian=guardian,
        defaults={"subject": subject},
    )
    if created:
        domain.record("communication.thread.opened", thread, kind=kind)
    return thread


@transaction.atomic
def post_message(actor: Actor, thread: Thread, *, text: str) -> Message:
    thread = (
        Thread.objects.select_for_update(of=("self",))
        .select_related("staff__membership", "guardian__membership", "student")
        .get(pk=thread.pk)
    )
    from_staff = thread.staff.membership_id == actor.membership.pk
    now = timezone.now()
    reply_seconds = None
    if from_staff:
        if thread.awaiting_reply_since is not None:
            reply_seconds = int((now - thread.awaiting_reply_since).total_seconds())
        thread.awaiting_reply_since = None
        others = [thread.guardian.membership] if thread.guardian and thread.guardian.membership else []
        if thread.kind == ThreadKind.STUDENT_TEACHER and thread.student.membership_id:
            others = [Membership.objects.get(pk=thread.student.membership_id)]
    else:
        if thread.awaiting_reply_since is None:
            thread.awaiting_reply_since = now
        others = [thread.staff.membership]
    message = Message.objects.create(
        school=actor.school,
        thread=thread,
        author=actor.membership,
        from_staff=from_staff,
        text=text,
        reply_seconds=reply_seconds,
    )
    thread.last_message_at = now
    thread.save(update_fields=["awaiting_reply_since", "last_message_at"])
    domain.record("communication.message.sent", thread, from_staff=from_staff)
    notifications.notify(
        actor.school,
        others,
        kind="message",
        title=f"New message about {thread.student.full_name}",
        body=text[:200],
        student=thread.student,
        link=("thread", thread.pk),
    )
    return message


def unanswered(school: Any, hours: int = REPLY_HOURS) -> QuerySet[Thread]:
    """Threads whose family message has waited at least ``hours`` ("Parent waiting 24h+")."""
    cutoff = timezone.now() - datetime.timedelta(hours=hours)
    return Thread.objects.filter(school_id=school.pk, awaiting_reply_since__lte=cutoff).select_related(
        "staff__membership__user", "student"
    )


def reply_hours(school: Any, since: datetime.datetime) -> dict[Any, float]:
    """Per teacher (staff profile): mean hours to answer a family message since ``since``."""
    rows = (
        Message.objects.filter(
            school_id=school.pk, from_staff=True, reply_seconds__isnull=False, at__gte=since
        )
        .values("thread__staff_id")
        .annotate(avg=Avg("reply_seconds"))
    )
    return {r["thread__staff_id"]: r["avg"] / 3600 for r in rows}


# ------------------------------------------------------------------------------------------------ complaints
@transaction.atomic
def raise_complaint(actor: Actor, *, student_id: Any, category: str, text: str, sentiment: str) -> Complaint:
    student = people_policies.students.queryset(actor, "complaint.create").filter(pk=student_id).first()
    if student is None:
        raise ValidationError({"student_id": ["Unknown student."]})
    complaint = Complaint.objects.create(
        school=actor.school,
        student=student,
        category=category,
        text=text,
        sentiment=sentiment,
        raised_by=actor.membership,
    )
    domain.record("communication.complaint.raised", complaint, category=category, sentiment=sentiment)
    return complaint


@transaction.atomic
def handle_complaint(actor: Actor, complaint: Complaint, **data: Any) -> Complaint:
    complaint = Complaint.objects.select_for_update().get(pk=complaint.pk)
    if complaint.status == ComplaintStatus.RESOLVED:
        raise Conflict("This complaint is resolved.")
    if "assigned_to_id" in data:
        mid = data.pop("assigned_to_id")
        data["assigned_to"] = (
            domain.resolve(Membership, actor.school, mid, "assigned_to_id", label="member") if mid else None
        )
    if data.get("status") == ComplaintStatus.RESOLVED:
        if not data.get("resolution"):
            raise ValidationError({"resolution": ["Say how it was resolved."]})
        data["resolved_at"] = timezone.now()
    changed = domain.apply_changes(complaint, data)
    if changed:
        complaint.save()
        domain.record("communication.complaint.updated", complaint, fields=changed, status=complaint.status)
    if complaint.status == ComplaintStatus.RESOLVED:
        notifications.notify(
            actor.school,
            [complaint.raised_by],
            kind="message",
            title=f"Your {complaint.get_category_display().lower()} complaint was resolved",
            body=complaint.resolution[:200],
            student=complaint.student,
            link=("complaint", complaint.pk),
        )
    return complaint


def sentiment_summary(school: Any, since: datetime.datetime) -> dict[str, Any]:
    """Counts by sentiment and by category (with open negatives), for the parent-sentiment dashboard."""
    rows = Complaint.objects.filter(school_id=school.pk, created_at__gte=since)
    by_sentiment = {r["sentiment"]: r["n"] for r in rows.values("sentiment").annotate(n=Count("id"))}
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "open_negative": 0})
    for r in rows.values("category", "sentiment", "status"):
        entry = by_category[r["category"]]
        entry["total"] += 1
        if r["sentiment"] == "negative" and r["status"] != ComplaintStatus.RESOLVED:
            entry["open_negative"] += 1
    total = sum(by_sentiment.values())
    score = (by_sentiment.get("positive", 0) - by_sentiment.get("negative", 0)) / total if total else None
    return {
        "total": total,
        "by_sentiment": {s: by_sentiment.get(s, 0) for s in ("negative", "neutral", "positive")},
        "by_category": dict(by_category),
        "net_sentiment": score,
    }
