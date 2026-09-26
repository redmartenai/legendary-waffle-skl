from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class Format(models.TextChoices):
    PDF = "pdf", "PDF"
    XLSX = "xlsx", "XLSX"


class CustomReport(SchoolScopedModel):
    """A report someone built from a module and a set of columns, saved to the library."""

    name = models.CharField(max_length=80)
    module = models.CharField(max_length=20)
    columns = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["name"]


class ScheduledReport(SchoolScopedModel):
    """A library report generated on a schedule and delivered in-app (and by email where the person has one)."""

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    name = models.CharField(max_length=80)
    report = models.CharField(max_length=40)  # a library key, or "custom:<uuid>"
    format = models.CharField(max_length=4, choices=Format.choices, default=Format.PDF)
    frequency = models.CharField(max_length=8, choices=Frequency.choices)
    weekday = models.PositiveSmallIntegerField(default=0, help_text="0 = Monday (weekly)")
    day_of_month = models.PositiveSmallIntegerField(default=1)
    at = models.TimeField()
    recipients = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="+")
    enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["created_at"]


class ReportRun(SchoolScopedModel):
    """One generated file: from the library (someone pressed Generate) or from a schedule."""

    report = models.CharField(max_length=40)
    custom = models.ForeignKey(CustomReport, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    title = models.CharField(max_length=120)
    format = models.CharField(max_length=4, choices=Format.choices)
    file = models.FileField(upload_to="reports/%Y/%m/")
    size = models.PositiveIntegerField(default=0)
    rows = models.PositiveIntegerField(default=0)
    filters = models.JSONField(default=dict, blank=True)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    schedule = models.ForeignKey(ScheduledReport, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")

    class Meta:
        ordering = ["-created_at"]


class ReportDelivery(SchoolScopedModel):
    """A scheduled run handed to one person on one channel (the Delivery log)."""

    schedule = models.ForeignKey(ScheduledReport, on_delete=models.CASCADE, related_name="deliveries")
    run = models.ForeignKey(ReportRun, on_delete=models.CASCADE, related_name="deliveries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    channel = models.CharField(max_length=8)  # in_app | email
    address = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=12, default="delivered")

    class Meta:
        ordering = ["-created_at"]
