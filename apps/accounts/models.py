import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core.models import SchoolScopedModel, TimeStampedModel
from apps.core.utils import first_name, initials, normalize_phone


class Role(models.TextChoices):
    PARENT = "parent", "Parent"
    STUDENT = "student", "Student"
    TEACHER = "teacher", "Teacher"
    PRINCIPAL = "principal", "Principal"
    ADMIN = "admin", "School admin"
    ACCOUNTANT = "accountant", "Accountant"
    TRANSPORT_MANAGER = "transport_manager", "Transport manager"
    DRIVER = "driver", "Driver"
    ATTENDANT = "attendant", "Bus attendant"


MANAGEMENT_ROLES = frozenset({Role.PRINCIPAL, Role.ADMIN})
TRANSPORT_STAFF_ROLES = frozenset({Role.PRINCIPAL, Role.ADMIN, Role.TRANSPORT_MANAGER})
CREW_ROLES = frozenset({Role.DRIVER, Role.ATTENDANT})
STAFF_ROLES = frozenset(
    {
        Role.TEACHER,
        Role.PRINCIPAL,
        Role.ADMIN,
        Role.ACCOUNTANT,
        Role.TRANSPORT_MANAGER,
        Role.DRIVER,
        Role.ATTENDANT,
    }
)
FAMILY_ROLES = frozenset({Role.PARENT, Role.STUDENT})


class Department(models.TextChoices):
    ACCOUNTS = "accounts", "Accounts"
    TRANSPORT = "transport", "Transport desk"
    ADMISSIONS = "admissions", "Admissions"
    OFFICE = "office", "Front office"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, phone, full_name="", password=None, **extra):
        user = self.model(phone=normalize_phone(phone), full_name=full_name, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, full_name="", password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(phone, full_name, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """One person, across every school they belong to. Signs in with a phone OTP."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone = models.CharField(max_length=16, unique=True)
    email = models.EmailField(blank=True)
    full_name = models.CharField(max_length=120)
    language = models.CharField(max_length=8, default="en")
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False, help_text="EduFlow platform staff (Django admin).")
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.phone})"

    @property
    def initials(self) -> str:
        return initials(self.full_name)

    @property
    def first_name(self) -> str:
        return first_name(self.full_name)


class Membership(SchoolScopedModel):
    """A user's role in one school. A person can hold several (e.g. teacher and parent)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=24, choices=Role.choices)
    title = models.CharField(max_length=80, blank=True)
    department = models.CharField(max_length=16, choices=Department.choices, blank=True)
    # Role-specific settings, e.g. {"office_hours": {"days": [0,1,2,3,4,5], "start": "15:30", "end": "17:00"}}
    settings = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["school", "user", "role"], name="uniq_membership_role"),
        ]

    def __str__(self):
        return f"{self.user.full_name} · {self.get_role_display()}"


class OtpChallenge(TimeStampedModel):
    """A one-time sign-in code. Only a keyed hash of the code is stored."""

    school = models.ForeignKey("tenancy.School", on_delete=models.CASCADE, related_name="+")
    phone = models.CharField(max_length=16, db_index=True)
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)


class PushDevice(TimeStampedModel):
    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"
        WEB = "web", "Web"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="push_devices")
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=Platform.choices)
    app_variant = models.CharField(max_length=20, default="main")
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
