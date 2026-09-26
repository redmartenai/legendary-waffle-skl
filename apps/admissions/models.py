from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import SchoolScopedModel


class AdmissionCycle(SchoolScopedModel):
    """One year's admissions: when enquiries opened, when applications close, the offer rounds and assessment days."""

    academic_year = models.CharField(max_length=20)  # "2027–28"
    enquiries_open_on = models.DateField()
    applications_close_on = models.DateField()
    session_starts_on = models.DateField()
    # [{"name": "Offer round 1", "on": "2026-09-15"}, ...]
    offer_rounds = models.JSONField(default=list, blank=True)
    is_current = models.BooleanField(default=True)

    class Meta:
        ordering = ["-session_starts_on"]
        constraints = [models.UniqueConstraint(fields=["school", "academic_year"], name="uniq_admission_cycle_year")]

    def __str__(self):
        return f"Admissions {self.academic_year}"


class SeatPlan(SchoolScopedModel):
    """New-admission seats for a grade (or a band of grades, e.g. "Grades 2–5") in one cycle."""

    cycle = models.ForeignKey(AdmissionCycle, on_delete=models.CASCADE, related_name="seat_plans")
    label = models.CharField(max_length=40)
    grades = models.JSONField(default=list, help_text='Grades this row covers, e.g. ["2", "3", "4", "5"]')
    seats = models.PositiveSmallIntegerField()
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]


class Application(SchoolScopedModel):
    """A family's journey from enquiry to admission.

    ``stage`` is where the child is on the board. ``status`` is the principal's decision through the approvals
    engine (the admission handler sets it); saving keeps the two in step, so an approval moves the card to Offer.
    """

    class Status(models.TextChoices):
        APPLIED = "applied", "Applied"
        OFFERED = "offered", "Offer made"
        DECLINED = "declined", "Declined"

    class Stage(models.TextChoices):
        ENQUIRY = "enquiry", "Enquiry"
        APPLICATION = "application", "Application"
        ASSESSMENT = "assessment", "Assessment"
        DOCUMENTS = "documents", "Documents"
        OFFER = "offer", "Offer"
        ADMITTED = "admitted", "Admitted"

    class Source(models.TextChoices):
        WEBSITE = "website", "Website"
        WALK_IN = "walk_in", "Walk-in"
        REFERRAL = "referral", "Referral"
        SOCIAL = "social", "Social media"

    class FollowUp(models.TextChoices):
        NONE = "", "None"
        CALL_BACK = "call_back", "Call back"
        TOUR = "tour", "School tour"
        PROSPECTUS = "prospectus", "Prospectus sent"

    application_no = models.CharField(max_length=30)
    child_name = models.CharField(max_length=120)
    grade = models.CharField(max_length=20)
    academic_year = models.CharField(max_length=20)
    guardian_name = models.CharField(max_length=120, blank=True)
    guardian_phone = models.CharField(max_length=16, blank=True)
    documents_verified = models.BooleanField(default=False)
    interaction_on = models.DateField(null=True, blank=True)
    sibling = models.ForeignKey("academics.Student", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPLIED)

    # --- the pipeline ---
    stage = models.CharField(max_length=12, choices=Stage.choices, default=Stage.ENQUIRY, db_index=True)
    stage_changed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.WALK_IN)
    enquired_on = models.DateField(null=True, blank=True)
    # Dropped out (or declined) at ``stage``; closed cards leave the board but still count in the funnel.
    closed = models.BooleanField(default=False)
    closed_reason = models.CharField(max_length=120, blank=True)
    follow_up = models.CharField(max_length=12, choices=FollowUp.choices, default=FollowUp.NONE, blank=True)
    follow_up_on = models.DateField(null=True, blank=True)
    form_fee_paid = models.BooleanField(default=False)
    documents_pending = models.CharField(max_length=80, blank=True, help_text="e.g. Report card, TC")
    assessment_at = models.DateTimeField(null=True, blank=True)
    assessment_score = models.PositiveSmallIntegerField(null=True, blank=True)
    assessment_out_of = models.PositiveSmallIntegerField(default=100)
    offer_made_on = models.DateField(null=True, blank=True)
    offer_reply_by = models.DateField(null=True, blank=True)
    offer_accepted_on = models.DateField(null=True, blank=True)
    fee_due_on = models.DateField(null=True, blank=True)
    fee_receipt_no = models.CharField(max_length=40, blank=True, help_text="Admission-fee receipt the office issued")
    admitted_on = models.DateField(null=True, blank=True)
    student = models.OneToOneField("academics.Student", null=True, blank=True, on_delete=models.SET_NULL, related_name="admission")

    class Meta:
        ordering = ["-created_at"]

    ORDER = [s.value for s in Stage]

    def stage_index(self) -> int:
        return self.ORDER.index(self.stage)

    def save(self, *args, **kwargs):
        touched = self._sync_stage_with_status()
        if touched and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | touched
        super().save(*args, **kwargs)

    def _sync_stage_with_status(self) -> set:
        """The approvals engine only knows ``status``; mirror its decision on the board."""
        today = timezone.localdate()
        if self.status == self.Status.OFFERED and self.stage_index() < self.ORDER.index(self.Stage.OFFER):
            self.stage, self.stage_changed_at, self.closed = self.Stage.OFFER, timezone.now(), False
            self.offer_made_on = self.offer_made_on or today
            return {"stage", "stage_changed_at", "closed", "offer_made_on"}
        if self.status == self.Status.APPLIED and self.stage == self.Stage.OFFER and not self.offer_accepted_on:
            # An approval that was undone: back to Documents, waiting for a decision again.
            self.stage, self.stage_changed_at, self.offer_made_on = self.Stage.DOCUMENTS, timezone.now(), None
            return {"stage", "stage_changed_at", "offer_made_on"}
        if self.status == self.Status.DECLINED and not self.closed:
            self.closed, self.closed_reason = True, self.closed_reason or "Not approved"
            return {"closed", "closed_reason"}
        if self.status == self.Status.APPLIED and self.closed and self.closed_reason == "Not approved":
            self.closed, self.closed_reason = False, ""
            return {"closed", "closed_reason"}
        return set()


class ApplicationEvent(SchoolScopedModel):
    """The history of one application: every move, booking, score, decision and note."""

    class Action(models.TextChoices):
        CREATED = "created", "Enquiry received"
        MOVED = "moved", "Moved"
        ASSESSMENT = "assessment", "Assessment booked"
        SCORED = "scored", "Assessment scored"
        VERIFIED = "verified", "Documents verified"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        ADMITTED = "admitted", "Admitted"
        CLOSED = "closed", "Closed"
        NOTE = "note", "Note"

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=12, choices=Action.choices)
    from_stage = models.CharField(max_length=12, blank=True)
    to_stage = models.CharField(max_length=12, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
