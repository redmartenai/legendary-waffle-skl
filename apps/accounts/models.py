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
    # Notification channels and alert switches, e.g. {"channels": {"sms": true}, "alerts": {"not_in_by": true}}
    preferences = models.JSONField(default=dict, blank=True)
    full_name = models.CharField(max_length=120)
    language = models.CharField(max_length=8, default="en")
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False, help_text="EduFlow platform staff (Django admin).")
    # Set when someone else issued a temporary password (e.g. a new school's principal): every API except
    # the password change refuses until the owner picks their own.
    must_change_password = models.BooleanField(default=False)
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
    """A one-time sign-in code. Only a keyed hash of the code is stored.

    `school` is set when the person typed a school code first; phone-first sign-in leaves it empty
    and the code then signs them in to every school they belong to."""

    school = models.ForeignKey("tenancy.School", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
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


class AuditLog(SchoolScopedModel):
    """Who did what, when, from where: exports, downloads, decisions, role and permission changes.

    Write entries with ``apps.accounts.audit.audit(request, action, ...)``. Entries are never edited.
    """

    actor = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Dotted verb, e.g. "fees.export", "document.download", "approval.approve", "role.update".
    action = models.CharField(max_length=60, db_index=True)
    module = models.CharField(max_length=30, blank=True, db_index=True)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=300, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    device = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["school", "module", "-created_at"])]


class CustomRole(SchoolScopedModel):
    """A role the school defined itself (e.g. Librarian). Its permissions are ``RolePermission`` rows under ``key``."""

    key = models.SlugField(max_length=40)
    name = models.CharField(max_length=60)
    description = models.CharField(max_length=200, blank=True)
    based_on = models.CharField(max_length=40, blank=True)
    members = models.ManyToManyField(User, blank=True, related_name="custom_roles")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["school", "key"], name="uniq_custom_role_key")]

    def __str__(self):
        return self.name


class RolePermission(SchoolScopedModel):
    """What one role may do in one module, and on whose data. Missing rows fall back to the built-in defaults
    (``apps.accounts.permissions.DEFAULTS``)."""

    class Scope(models.TextChoices):
        SCHOOL = "school", "Entire school"
        CLASSES = "classes", "Assigned classes"
        OWN = "own", "Own record"
        NONE = "none", "No access"

    # A system role (``Role``) or a ``CustomRole.key``.
    role = models.CharField(max_length=40)
    module = models.CharField(max_length=20)
    can_view = models.BooleanField(default=False)
    can_create = models.BooleanField(default=False)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_approve = models.BooleanField(default=False)
    can_export = models.BooleanField(default=False)
    can_download = models.BooleanField(default=False)
    can_publish = models.BooleanField(default=False)
    data_scope = models.CharField(max_length=8, choices=Scope.choices, default=Scope.NONE)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    ACTIONS = ("view", "create", "edit", "delete", "approve", "export", "download", "publish")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "role", "module"], name="uniq_role_permission")]

    def cell(self) -> dict:
        return {**{a: getattr(self, f"can_{a}") for a in self.ACTIONS}, "data_scope": self.data_scope}

    def set_cell(self, cell: dict) -> None:
        for a in self.ACTIONS:
            setattr(self, f"can_{a}", bool(cell.get(a)))
        self.data_scope = cell.get("data_scope", self.data_scope)
