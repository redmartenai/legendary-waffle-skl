"""Platform-level records (not school-scoped): invites and the credential hand-over log."""

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class SchoolInvite(TimeStampedModel):
    """A single-use link for a school's principal/admin to set their own password. Only a hash is stored."""

    school = models.ForeignKey("tenancy.School", on_delete=models.CASCADE, related_name="invites")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="school_invites")
    role = models.CharField(max_length=20)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]


class CredentialIssue(TimeStampedModel):
    """Every time EduFlow staff issued, reset or re-sent someone's sign-in for a school."""

    class Method(models.TextChoices):
        PASSWORD = "password", "Temporary password"
        INVITE = "invite", "Invite link"
        EXISTING = "existing", "Existing account"

    school = models.ForeignKey("tenancy.School", on_delete=models.CASCADE, related_name="credential_issues")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=20)
    method = models.CharField(max_length=10, choices=Method.choices)
    reason = models.CharField(max_length=20, default="created")  # created | reset | added
    delivered = models.JSONField(default=dict, blank=True)  # {"sms": "logged", "email": "no_address"}
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]
