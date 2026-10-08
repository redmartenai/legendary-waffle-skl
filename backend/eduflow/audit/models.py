"""The audit trail (ADR-015, docs/security/audit.md).

Append-only: a trigger rejects UPDATE and DELETE for every role, and the application role is not even
granted them (audit migration 0002). References are plain UUIDs rather than foreign keys, so the trail
outlives the rows it describes and deleting a user never rewrites history.
"""

from __future__ import annotations

from django.db import models

from eduflow.core.ids import uuid7


class Outcome(models.TextChoices):
    SUCCESS = "success", "Success"
    FAILURE = "failure", "Failure"
    DENIED = "denied", "Denied"


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True)
    school_id = models.UUIDField(null=True, blank=True, help_text="Null for platform and sign-in events.")
    actor_id = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=64)
    outcome = models.CharField(max_length=8, choices=Outcome.choices, default=Outcome.SUCCESS)
    target_type = models.CharField(max_length=48, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    request_id = models.CharField(max_length=128, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "audit_event"
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["school_id", "-occurred_at"], name="audit_event_school_time_idx"),
            models.Index(fields=["school_id", "target_type", "target_id"], name="audit_event_target_idx"),
            models.Index(fields=["actor_id", "-occurred_at"], name="audit_event_actor_time_idx"),
            models.Index(fields=["action", "-occurred_at"], name="audit_event_action_time_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(outcome__in=["success", "failure", "denied"]),
                name="audit_event_outcome_check",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.outcome}"
