from django.db import models

from apps.core.models import TimeStampedModel

DEFAULT_POLICIES = {
    "chat": {
        # Parents may message the class teacher always, subject teachers if enabled.
        "parent_to_subject_teachers": True,
        # 1:1 chat with students is off for schools (safeguarding); colleges can enable.
        "student_chat": False,
        "read_receipts": True,
    },
    "transport": {
        "default_alert_minutes": 10,
        "speed_limit_kmh": 40,
        "share_live_location_with_parents": True,
    },
    "notifications": {"quiet_hours": ["21:00", "07:00"]},
}


class Organization(TimeStampedModel):
    """A trust, society or school group. The paying customer."""

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=80, unique=True)

    def __str__(self):
        return self.name


class School(TimeStampedModel):
    """One institution (school or college). Most data is scoped to a school."""

    class Kind(models.TextChoices):
        SCHOOL = "school", "School"
        JUNIOR_COLLEGE = "junior_college", "Junior college"
        COLLEGE = "college", "College"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="schools")
    code = models.CharField(max_length=16, unique=True, help_text="The code people type to find the school.")
    name = models.CharField(max_length=160)
    short_name = models.CharField(max_length=40, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SCHOOL)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=80, blank=True)
    timezone = models.CharField(max_length=40, default="Asia/Kolkata")
    primary_color = models.CharField(max_length=7, default="#2E5D4E")
    accent_color = models.CharField(max_length=7, default="#B4763A")
    logo_url = models.URLField(blank=True)
    languages = models.JSONField(default=list, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def policy(self, section: str, key: str):
        section_values = {**DEFAULT_POLICIES.get(section, {}), **self.settings.get(section, {})}
        return section_values.get(key)

    @property
    def branding(self) -> dict:
        return {
            "primary_color": self.primary_color,
            "accent_color": self.accent_color,
            "logo_url": self.logo_url or None,
        }
