"""Admissions: the application pipeline from enquiry to enrolled student (prototype ``Admission``; screen
documentation "Admissions"; approvals queue "admissions").

Pipeline (the prototype's stages)::

    enquiry -> visit -> assessment -> offer --(approval)--> enrolled
       \\________\\__________\\_________\\--> dropped

* A stage may be skipped forwards (a walk-in can go straight to assessment); no stage goes backwards.
* An **offer** is decided in the approvals queue. Only an approved offer can be enrolled; enrolling creates
  the student, guardian and enrollment through the ``people`` services (no second student model).
* Every stage change is a ``StageChange`` row (who, when, note) and an audit event.
* Online applications (``source = website``) arrive through the public apply endpoint as enquiries.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.academics.models import AcademicYear, Grade
from eduflow.core.ids import uuid7
from eduflow.documents.models import StoredFile
from eduflow.people.models import Enrollment, Guardian, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class Stage(models.TextChoices):
    ENQUIRY = "enquiry", "Enquiry"
    VISIT = "visit", "Visit"
    ASSESSMENT = "assessment", "Assessment"
    OFFER = "offer", "Offer"
    ENROLLED = "enrolled", "Enrolled"
    DROPPED = "dropped", "Dropped"


ORDER = [Stage.ENQUIRY, Stage.VISIT, Stage.ASSESSMENT, Stage.OFFER, Stage.ENROLLED]


class Source(models.TextChoices):
    WEBSITE = "website", "Website"
    WALK_IN = "walk_in", "Walk-in"
    REFERRAL = "referral", "Referral"
    SOCIAL = "social", "Social"
    NEWSPAPER = "newspaper", "Newspaper"


class OfferDecision(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"


class Application(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    child_name = models.CharField(max_length=200)
    date_of_birth = models.DateField(null=True, blank=True)
    grade = models.ForeignKey(Grade, on_delete=models.PROTECT, related_name="+")
    academic_year = models.ForeignKey(
        AcademicYear, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    parent_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    source = models.CharField(max_length=16, choices=Source.choices)
    stage = models.CharField(max_length=16, choices=Stage.choices, default=Stage.ENQUIRY)
    notes = models.CharField(max_length=2000, blank=True)
    fee_quoted = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    offer_decision = models.CharField(max_length=16, choices=OfferDecision.choices, blank=True)
    offer_requested_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    offer_requested_at = models.DateTimeField(null=True, blank=True)
    offer_decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    offer_decided_at = models.DateTimeField(null=True, blank=True)
    student = models.OneToOneField(Student, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    guardian = models.ForeignKey(Guardian, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    enrollment = models.OneToOneField(
        Enrollment, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "admissions_application"
        constraints = [
            models.CheckConstraint(condition=Q(stage__in=Stage.values), name="admissions_stage_check"),
            models.CheckConstraint(
                condition=Q(fee_quoted__isnull=True) | Q(fee_quoted__gte=0), name="admissions_fee_check"
            ),
            models.CheckConstraint(
                condition=~Q(stage=Stage.ENROLLED) | (Q(student__isnull=False) & Q(enrollment__isnull=False)),
                name="admissions_enrolled_has_student_check",
            ),
            models.CheckConstraint(
                condition=~Q(stage=Stage.ENROLLED) | Q(offer_decision=OfferDecision.APPROVED),
                name="admissions_enrolled_needs_approval_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="admissions_application_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["school", "stage"], name="admissions_stage_idx")]


class StageChange(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="history")
    from_stage = models.CharField(max_length=16, choices=Stage.choices, blank=True)
    to_stage = models.CharField(max_length=16, choices=Stage.choices)
    note = models.CharField(max_length=500, blank=True)
    by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "admissions_stage_change"
        constraints = [
            models.UniqueConstraint(fields=["id", "school"], name="admissions_stage_change_id_school_uniq")
        ]


class ApplicationDocument(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=200)
    file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "admissions_document"
        constraints = [
            models.UniqueConstraint(fields=["id", "school"], name="admissions_document_id_school_uniq")
        ]
