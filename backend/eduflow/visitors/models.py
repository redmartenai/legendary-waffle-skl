"""Visitor management (product scope: "visitor management with QR passes and security approval").

* A **visit** is pre-registered by a host (a staff member or a parent) or registered at the gate by
  security. A pre-registered visit waits for **security approval**; a gate registration is approved at once.
* An approved visit gets a **pass**: a random token shown to the visitor as a QR code. Only the token's
  SHA-256 is stored, so a database leak does not leak passes. Issuing a new pass invalidates the old one.
* At the gate, security scans the token: the first scan checks the visitor in, the next checks them out.
  A pass works only on the visit's day (school time) and only while the visit is approved or inside.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.core.ids import uuid7
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class VisitStatus(models.TextChoices):
    PENDING = "pending", "Pending approval"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    INSIDE = "inside", "Checked in"
    LEFT = "left", "Checked out"
    CANCELLED = "cancelled", "Cancelled"


class Visit(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    visitor_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32)
    purpose = models.CharField(max_length=300)
    visitors_count = models.PositiveSmallIntegerField(default=1)
    vehicle_number = models.CharField(max_length=20, blank=True)
    id_proof = models.CharField(max_length=100, blank=True, help_text="Kind and last digits only.")
    expected_on = models.DateField()
    host = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=16, choices=VisitStatus.choices, default=VisitStatus.PENDING)
    registered_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    pass_hash = models.CharField(max_length=64, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "visitors_visit"
        constraints = [
            models.UniqueConstraint(
                fields=["pass_hash"], condition=~Q(pass_hash=""), name="visitors_pass_uniq"
            ),
            models.CheckConstraint(condition=Q(visitors_count__gte=1), name="visitors_min_count_check"),
            models.CheckConstraint(
                condition=~Q(status="inside") | Q(checked_in_at__isnull=False), name="visitors_inside_check"
            ),
            models.UniqueConstraint(fields=["id", "school"], name="visitors_visit_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["school", "expected_on"], name="visitors_day_idx")]
