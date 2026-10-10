"""Remarks and behaviour incidents (prototype ``Remark`` and ``behaviourIncidents``; monitoring rule
"Repeated behaviour notes": 3 or more incidents in the current term).

* A **remark** is a teacher's note about a student, with a tone (praise, concern, note) and an optional
  subject (none means "General"). Families see it only when ``visible_to_family`` is set.
* A **behaviour incident** records what happened, its severity and the action taken; it is resolved once
  handled. Families see it only when ``visible_to_family`` is set.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.academics.models import Subject
from eduflow.core.ids import uuid7
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class Tone(models.TextChoices):
    PRAISE = "praise", "Praise"
    CONCERN = "concern", "Concern"
    NOTE = "note", "Note"


class Severity(models.TextChoices):
    MINOR = "minor", "Minor"
    MODERATE = "moderate", "Moderate"
    SERIOUS = "serious", "Serious"


class IncidentStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class Remark(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="remarks")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    tone = models.CharField(max_length=16, choices=Tone.choices)
    text = models.CharField(max_length=2000)
    visible_to_family = models.BooleanField(default=True)
    author = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "conduct_remark"
        constraints = [
            models.CheckConstraint(condition=Q(tone__in=Tone.values), name="conduct_remark_tone_check"),
            models.UniqueConstraint(fields=["id", "school"], name="conduct_remark_id_school_uniq"),
        ]


class Incident(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="incidents")
    occurred_on = models.DateField()
    category = models.CharField(max_length=60)
    description = models.CharField(max_length=2000)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    action_taken = models.CharField(max_length=1000, blank=True)
    visible_to_family = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=IncidentStatus.choices, default=IncidentStatus.OPEN)
    reported_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    resolved_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "conduct_incident"
        constraints = [
            models.CheckConstraint(condition=Q(severity__in=Severity.values), name="conduct_severity_check"),
            models.CheckConstraint(
                condition=Q(status=IncidentStatus.OPEN) | Q(resolved_at__isnull=False),
                name="conduct_resolved_has_time_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="conduct_incident_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["school", "occurred_on"], name="conduct_incident_date_idx")]
