"""Hostels (screen documentation "Hostel": rooms, allocation, outpass, attendance; role "Hostel Manager").

* A **hostel** has **rooms** with a bed capacity. A student holds at most one active **allocation**; a room
  never holds more active allocations than beds (checked under a row lock).
* An **outpass** lets a boarder leave: requested by the family, the student or the warden, decided in the
  approvals queue (``outpass``), then checked out and back in at the gate. Returning after ``return_by`` is
  recorded as late.
* The nightly **roll call** records each boarder present, absent or on outpass.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from eduflow.core.ids import uuid7
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _uniq(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"hostel_{model}_id_school_uniq")


class Hostel(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100)
    warden = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hostel_hostel"
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="hostel_name_uniq"),
            _uniq("hostel"),
        ]


class Room(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hostel = models.ForeignKey(Hostel, on_delete=models.PROTECT, related_name="rooms")
    number = models.CharField(max_length=20)
    beds = models.PositiveSmallIntegerField()

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hostel_room"
        constraints = [
            models.UniqueConstraint(fields=["hostel", "number"], name="hostel_room_number_uniq"),
            models.CheckConstraint(condition=Q(beds__gte=1), name="hostel_room_min_beds_check"),
            _uniq("room"),
        ]


class Allocation(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="hostel_allocations")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="allocations")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hostel_allocation"
        constraints = [
            models.UniqueConstraint(
                fields=["student"], condition=Q(end_date__isnull=True), name="hostel_one_active_allocation"
            ),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="hostel_allocation_dates_check",
            ),
            _uniq("allocation"),
        ]


class OutpassStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    OUT = "out", "Out"
    RETURNED = "returned", "Returned"
    CANCELLED = "cancelled", "Cancelled"


class Outpass(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="outpasses")
    leave_at = models.DateTimeField()
    return_by = models.DateTimeField()
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=16, choices=OutpassStatus.choices, default=OutpassStatus.PENDING)
    requested_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hostel_outpass"
        constraints = [
            models.CheckConstraint(
                condition=Q(return_by__gt=F("leave_at")), name="hostel_outpass_window_check"
            ),
            _uniq("outpass"),
        ]


class RollStatus(models.TextChoices):
    PRESENT = "present", "Present"
    ABSENT = "absent", "Absent"
    ON_OUTPASS = "on_outpass", "On outpass"


class RollCall(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="hostel_roll")
    hostel = models.ForeignKey(Hostel, on_delete=models.PROTECT, related_name="roll")
    date = models.DateField()
    status = models.CharField(max_length=16, choices=RollStatus.choices)
    recorded_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hostel_roll_call"
        constraints = [
            models.UniqueConstraint(fields=["student", "date"], name="hostel_roll_uniq"),
            _uniq("roll_call"),
        ]
