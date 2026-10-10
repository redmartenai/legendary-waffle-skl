"""Hostel writes. Transactional and audited (``hostel.*``)."""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people import policies as people_policies
from eduflow.people.models import Student
from eduflow.tenancy import clock, domain
from eduflow.tenancy.models import Membership

from .models import Allocation, Hostel, Outpass, OutpassStatus, RollCall, Room


@transaction.atomic
def create_hostel(
    actor: Actor, *, name: str, warden_id: Any = None, rooms: list[dict[str, Any]] | None = None
) -> Hostel:
    warden = (
        domain.resolve(Membership, actor.school, warden_id, "warden_id", label="member")
        if warden_id
        else None
    )
    hostel = domain.save(
        Hostel(school=actor.school, name=name, warden=warden), conflict="This hostel exists."
    )
    for room in rooms or []:
        domain.save(
            Room(school=actor.school, hostel=hostel, **room), conflict=f"Room {room['number']} repeats."
        )
    domain.record("hostel.hostel.created", hostel, rooms=len(rooms or []))
    return hostel


@transaction.atomic
def add_room(actor: Actor, hostel: Hostel, *, number: str, beds: int) -> Room:
    room = domain.save(
        Room(school=actor.school, hostel=hostel, number=number, beds=beds), conflict="This room exists."
    )
    domain.record("hostel.room.created", room, hostel=str(hostel.pk))
    return room


@transaction.atomic
def allocate(
    actor: Actor, *, student_id: Any, room_id: Any, start_date: datetime.date | None = None
) -> Allocation:
    student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
    room = Room.objects.select_for_update().filter(school_id=actor.school.pk, pk=room_id).first()
    if room is None:
        raise ValidationError({"room_id": ["Unknown room."]})
    if room.allocations.filter(end_date__isnull=True).count() >= room.beds:
        raise Conflict(f"Room {room.number} is full.")
    today = clock.today(actor.school)
    Allocation.objects.filter(student=student, end_date__isnull=True).update(end_date=start_date or today)
    allocation = Allocation.objects.create(
        school=actor.school, student=student, room=room, start_date=start_date or today
    )
    domain.record("hostel.allocation.created", allocation, student=str(student.pk), room=str(room.pk))
    return allocation


@transaction.atomic
def vacate(actor: Actor, allocation: Allocation) -> Allocation:
    if allocation.end_date is not None:
        raise Conflict("This allocation has already ended.")
    allocation.end_date = clock.today(actor.school)
    allocation.save(update_fields=["end_date"])
    domain.record("hostel.allocation.ended", allocation)
    return allocation


def _boarder(student: Student) -> Allocation | None:
    return (
        Allocation.objects.filter(student=student, end_date__isnull=True)
        .select_related("room__hostel")
        .first()
    )


@transaction.atomic
def request_outpass(
    actor: Actor, *, student_id: Any, leave_at: datetime.datetime, return_by: datetime.datetime, reason: str
) -> Outpass:
    """A parent for their child, a student for themselves, or the hostel office for any boarder."""
    student = people_policies.students.queryset(actor, "hostel.outpass").filter(pk=student_id).first()
    if student is None:
        raise ValidationError({"student_id": ["Unknown student."]})
    if _boarder(student) is None:
        raise ValidationError({"student_id": ["This student does not live in a hostel."]})
    if return_by <= leave_at:
        raise ValidationError({"return_by": ["The return is before the departure."]})
    if leave_at < timezone.now() - datetime.timedelta(hours=1):
        raise ValidationError({"leave_at": ["The departure is in the past."]})
    outpass = Outpass.objects.create(
        school=actor.school,
        student=student,
        leave_at=leave_at,
        return_by=return_by,
        reason=reason,
        requested_by=actor.membership,
    )
    domain.record("hostel.outpass.requested", outpass, student=str(student.pk))
    return outpass


@transaction.atomic
def decide_outpass(actor: Actor, outpass: Outpass, decision: str, note: str = "") -> Outpass:
    outpass = Outpass.objects.select_for_update(of=("self",)).select_related("student").get(pk=outpass.pk)
    if outpass.status != OutpassStatus.PENDING:
        raise Conflict("This outpass has already been decided.")
    outpass.status = OutpassStatus.APPROVED if decision == "approve" else OutpassStatus.DECLINED
    outpass.decided_by, outpass.decided_at, outpass.decision_note = actor.membership, timezone.now(), note
    outpass.save()
    domain.record("hostel.outpass.decided", outpass, decision=outpass.status)
    notifications.notify(
        actor.school,
        notifications.family_of(outpass.student),
        kind="approval",
        title=f"Outpass {outpass.status}",
        body=note,
        student=outpass.student,
        link=("outpass", outpass.pk),
    )
    return outpass


@transaction.atomic
def gate(actor: Actor, outpass: Outpass, *, action: str) -> Outpass:
    """``check_out`` an approved outpass, or ``check_in`` a student who is out."""
    outpass = Outpass.objects.select_for_update(of=("self",)).select_related("student").get(pk=outpass.pk)
    now = timezone.now()
    if action == "check_out":
        if outpass.status != OutpassStatus.APPROVED:
            raise Conflict("Only an approved outpass can be used.")
        outpass.status, outpass.checked_out_at = OutpassStatus.OUT, now
    else:
        if outpass.status != OutpassStatus.OUT:
            raise Conflict("This student is not out on this outpass.")
        outpass.status, outpass.returned_at = OutpassStatus.RETURNED, now
    outpass.save()
    late = action == "check_in" and now > outpass.return_by
    domain.record(f"hostel.outpass.{action}", outpass, late=late or None)
    notifications.notify(
        actor.school,
        notifications.family_of(outpass.student),
        kind="approval",
        title="Left the hostel"
        if action == "check_out"
        else ("Returned late" if late else "Returned to the hostel"),
        student=outpass.student,
        link=("outpass", outpass.pk),
    )
    return outpass


@transaction.atomic
def cancel_outpass(actor: Actor, outpass: Outpass) -> Outpass:
    if outpass.requested_by_id != actor.membership.pk and DataScope.SCHOOL not in actor.scopes(
        "hostel.manage"
    ):
        raise PermissionDenied("Only the requester or the hostel office can cancel an outpass.")
    outpass = Outpass.objects.select_for_update().get(pk=outpass.pk)
    if outpass.status not in (OutpassStatus.PENDING, OutpassStatus.APPROVED):
        raise Conflict("This outpass can no longer be cancelled.")
    outpass.status = OutpassStatus.CANCELLED
    outpass.save(update_fields=["status"])
    domain.record("hostel.outpass.cancelled", outpass)
    return outpass


@transaction.atomic
def roll_call(actor: Actor, hostel: Hostel, *, date: datetime.date, entries: Iterable[dict[str, Any]]) -> int:
    if date > clock.today(actor.school):
        raise ValidationError({"date": ["The roll call cannot be in the future."]})
    boarders = set(
        Allocation.objects.filter(room__hostel=hostel, end_date__isnull=True).values_list(
            "student_id", flat=True
        )
    )
    rows = list(entries)
    for i, e in enumerate(rows):
        if e["student_id"] not in boarders:
            raise ValidationError(
                {f"entries[{i}].student_id": ["This student does not board in this hostel."]}
            )
    for e in rows:
        RollCall.objects.update_or_create(
            school_id=actor.school.pk,
            student_id=e["student_id"],
            date=date,
            defaults={"hostel": hostel, "status": e["status"], "recorded_by": actor.membership},
        )
    domain.record("hostel.roll_call.recorded", hostel, date=date.isoformat(), count=len(rows))
    return len(rows)


def overdue_outpasses(school: Any) -> list[Outpass]:
    """Students still out after their return time (a monitoring input)."""
    return list(
        Outpass.objects.filter(school_id=school.pk, status=OutpassStatus.OUT, return_by__lt=timezone.now())
        .select_related("student")
        .order_by("return_by")
    )
