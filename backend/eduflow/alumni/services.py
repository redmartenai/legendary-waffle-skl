"""Alumni writes. Transactional and audited (``alumni.*``)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import connection, transaction
from django.db.models import Max, Sum
from rest_framework.exceptions import ValidationError

from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.people.models import Student
from eduflow.tenancy import clock, domain

from .models import Alumnus, Campaign, Donation, Event, Registration


@transaction.atomic
def add_alumnus(actor: Actor, *, student_id: Any = None, **data: Any) -> Alumnus:
    student = (
        domain.resolve(Student, actor.school, student_id, "student_id", label="student")
        if student_id
        else None
    )
    if student is not None and not data.get("full_name"):
        data["full_name"] = student.full_name
    alumnus = domain.save(
        Alumnus(school=actor.school, student=student, **data), conflict="This student is already an alumnus."
    )
    domain.record("alumni.alumnus.created", alumnus, year=alumnus.graduation_year)
    return alumnus


@transaction.atomic
def update_alumnus(actor: Actor, alumnus: Alumnus, **data: Any) -> Alumnus:
    changed = domain.apply_changes(alumnus, data)
    if changed:
        alumnus.save()
        domain.record("alumni.alumnus.updated", alumnus, fields=changed)
    return alumnus


@transaction.atomic
def create_event(actor: Actor, **data: Any) -> Event:
    event = Event.objects.create(school=actor.school, **data)
    domain.record("alumni.event.created", event, title=event.title)
    return event


@transaction.atomic
def register(actor: Actor, event: Event, *, alumnus_id: Any, guests: int = 0) -> Registration:
    alumnus = domain.resolve(Alumnus, actor.school, alumnus_id, "alumnus_id", label="alumnus")
    event = Event.objects.select_for_update().get(pk=event.pk)
    if event.capacity is not None:
        taken = event.registrations.aggregate(n=Sum("guests"))["n"] or 0
        if event.registrations.count() + taken + 1 + guests > event.capacity:
            raise Conflict("The event is full.")
    registration = domain.save(
        Registration(school=actor.school, event=event, alumnus=alumnus, guests=guests),
        conflict="This alumnus is already registered.",
    )
    domain.record("alumni.event.registered", registration, event=str(event.pk))
    return registration


@transaction.atomic
def create_campaign(actor: Actor, **data: Any) -> Campaign:
    if data["ends_on"] < data["starts_on"]:
        raise ValidationError({"ends_on": ["The campaign ends before it starts."]})
    campaign = Campaign.objects.create(school=actor.school, **data)
    domain.record("alumni.campaign.created", campaign, goal=str(campaign.goal_amount))
    return campaign


def raised(campaign: Campaign) -> Decimal:
    return campaign.donations.aggregate(t=Sum("amount"))["t"] or Decimal(0)


@transaction.atomic
def record_donation(actor: Actor, campaign: Campaign, *, alumnus_id: Any = None, **data: Any) -> Donation:
    alumnus = (
        domain.resolve(Alumnus, actor.school, alumnus_id, "alumnus_id", label="alumnus")
        if alumnus_id
        else None
    )
    if alumnus is not None and not data.get("donor_name"):
        data["donor_name"] = alumnus.full_name
    if not data.get("donor_name"):
        raise ValidationError({"donor_name": ["Give the donor's name."]})
    if data["received_on"] > clock.today(actor.school):
        raise ValidationError({"received_on": ["A donation cannot be dated in the future."]})
    # Receipt numbers are sequential per school: a transaction-scoped advisory lock serialises issuing.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"alumni-receipt:{actor.school.pk}"])
    last = Donation.objects.filter(school_id=actor.school.pk).aggregate(n=Max("receipt_number"))["n"]
    number = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    donation = Donation.objects.create(
        school=actor.school,
        campaign=campaign,
        alumnus=alumnus,
        receipt_number=f"D-{number:06d}",
        recorded_by=actor.membership,
        **data,
    )
    domain.record(
        "alumni.donation.recorded", donation, amount=str(donation.amount), campaign=str(campaign.pk)
    )
    return donation
