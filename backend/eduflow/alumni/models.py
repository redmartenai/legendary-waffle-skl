"""Alumni (product scope: alumni relations).

* An **alumnus** record holds a former student's contact and career details, optionally linked to their
  student record. ``consent_to_contact`` must be true before the school contacts them; the API filters on
  it and campaign/event outreach uses only consenting alumni.
* **Events** (reunions, talks) take **registrations**.
* **Campaigns** collect **donations**, each with its own receipt number. Donations are recorded by the
  office; EduFlow takes no online payments.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from eduflow.core.ids import uuid7
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _uniq(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"alumni_{model}_id_school_uniq")


class Alumnus(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.OneToOneField(
        Student, on_delete=models.PROTECT, null=True, blank=True, related_name="alumnus"
    )
    full_name = models.CharField(max_length=200)
    graduation_year = models.PositiveSmallIntegerField()
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    occupation = models.CharField(max_length=150, blank=True)
    organisation = models.CharField(max_length=150, blank=True)
    city = models.CharField(max_length=100, blank=True)
    consent_to_contact = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "alumni_alumnus"
        constraints = [
            models.CheckConstraint(
                condition=Q(graduation_year__gte=1900) & Q(graduation_year__lte=2200),
                name="alumni_year_check",
            ),
            _uniq("alumnus"),
        ]


class Event(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    venue = models.CharField(max_length=200, blank=True)
    description = models.TextField(max_length=5000, blank=True)
    capacity = models.PositiveIntegerField(null=True, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "alumni_event"
        constraints = [_uniq("event")]


class Registration(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="registrations")
    alumnus = models.ForeignKey(Alumnus, on_delete=models.CASCADE, related_name="registrations")
    guests = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "alumni_registration"
        constraints = [
            models.UniqueConstraint(fields=["event", "alumnus"], name="alumni_registration_uniq"),
            _uniq("registration"),
        ]


class Campaign(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    title = models.CharField(max_length=200)
    purpose = models.CharField(max_length=1000, blank=True)
    goal_amount = models.DecimalField(max_digits=14, decimal_places=2)
    starts_on = models.DateField()
    ends_on = models.DateField()

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "alumni_campaign"
        constraints = [
            models.CheckConstraint(condition=Q(goal_amount__gt=0), name="alumni_goal_check"),
            models.CheckConstraint(
                condition=Q(ends_on__gte=F("starts_on")), name="alumni_campaign_dates_check"
            ),
            _uniq("campaign"),
        ]


class Donation(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="donations")
    alumnus = models.ForeignKey(
        Alumnus, on_delete=models.PROTECT, null=True, blank=True, related_name="donations"
    )
    donor_name = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    received_on = models.DateField()
    mode = models.CharField(max_length=30)
    reference = models.CharField(max_length=100, blank=True)
    receipt_number = models.CharField(max_length=32)
    recorded_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "alumni_donation"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="alumni_donation_amount_check"),
            models.UniqueConstraint(fields=["school", "receipt_number"], name="alumni_receipt_uniq"),
            _uniq("donation"),
        ]
