"""People: staff and teachers, students, guardians, enrollments and teacher assignments.

Identity stays in ``identity`` (credentials, OTP) and school access in ``tenancy`` (membership). A profile
that belongs to someone who signs in points at their **membership in this school**, never at a bare user:
composite foreign keys ``(membership_id, school_id)`` make a profile in one school pointing at a membership
in another impossible, and data scopes resolve from ``actor.membership`` directly.

Every table is school-owned and protected by RLS (people migration 0002).
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.academics.models import AcademicYear, Campus, Department, Grade, Section, Subject
from eduflow.core.ids import uuid7
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _same_school_target(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"people_{model}_id_school_uniq")


# ---------------------------------------------------------------------------------------------- staff
class StaffType(models.TextChoices):
    TEACHING = "teaching", "Teaching"
    NON_TEACHING = "non_teaching", "Non-teaching"


class StaffStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ON_LEAVE = "on_leave", "On leave"
    LEFT = "left", "Left"


class StaffProfile(TenantModel):
    """A staff member's school record. A teacher is a ``teaching`` profile plus the teacher role."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    membership = models.OneToOneField(Membership, on_delete=models.PROTECT, related_name="staff_profile")
    employee_id = models.CharField(max_length=32)
    staff_type = models.CharField(max_length=16, choices=StaffType.choices, default=StaffType.TEACHING)
    designation = models.CharField(max_length=100, blank=True)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="staff"
    )
    campus = models.ForeignKey(Campus, on_delete=models.PROTECT, null=True, blank=True, related_name="staff")
    joining_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=StaffStatus.choices, default=StaffStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "people_staff_profile"
        constraints = [
            models.UniqueConstraint(fields=["school", "employee_id"], name="people_staff_employee_id_uniq"),
            _same_school_target("staff_profile"),
        ]
        indexes = [models.Index(fields=["school", "staff_type", "status"], name="people_staff_type_idx")]

    def __str__(self) -> str:
        return self.employee_id


# ---------------------------------------------------------------------------------------------- students
class Gender(models.TextChoices):
    FEMALE = "female", "Female"
    MALE = "male", "Male"
    OTHER = "other", "Other"
    UNDISCLOSED = "undisclosed", "Prefer not to say"


class StudentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    GRADUATED = "graduated", "Graduated"
    LEFT = "left", "Left the school"


class Student(TenantModel):
    """A pupil of the school. ``membership`` is set only for students who sign in themselves."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    membership = models.OneToOneField(
        Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="student_profile"
    )
    admission_number = models.CharField(max_length=32)
    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=16, choices=Gender.choices, blank=True)
    admission_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=StudentStatus.choices, default=StudentStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "people_student"
        constraints = [
            models.UniqueConstraint(
                fields=["school", "admission_number"], name="people_student_admission_uniq"
            ),
            _same_school_target("student"),
        ]
        indexes = [models.Index(fields=["school", "status", "last_name"], name="people_student_status_idx")]

    def __str__(self) -> str:
        return self.admission_number

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)


# ---------------------------------------------------------------------------------------------- guardians
class Guardian(TenantModel):
    """A parent or guardian as the school knows them. ``membership`` is set when they sign in.

    Contact details live here because many guardians never have an account; when they do, their identity
    (and sign-in) is still the Phase 2 user behind the membership.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    membership = models.OneToOneField(
        Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="guardian_profile"
    )
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=16, blank=True)
    email = models.EmailField(blank=True)
    occupation = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "people_guardian"
        constraints = [_same_school_target("guardian")]
        indexes = [models.Index(fields=["school", "phone"], name="people_guardian_phone_idx")]

    def __str__(self) -> str:
        return str(self.id)


class GuardianRelationship(models.TextChoices):
    MOTHER = "mother", "Mother"
    FATHER = "father", "Father"
    GUARDIAN = "guardian", "Legal guardian"
    GRANDPARENT = "grandparent", "Grandparent"
    SIBLING = "sibling", "Sibling"
    OTHER = "other", "Other"


class ContactPreference(models.TextChoices):
    PHONE = "phone", "Phone"
    SMS = "sms", "SMS"
    EMAIL = "email", "Email"
    APP = "app", "App"


class StudentGuardian(TenantModel):
    """Links a student to a guardian: many guardians per student, many students per guardian."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="guardian_links")
    guardian = models.ForeignKey(Guardian, on_delete=models.CASCADE, related_name="student_links")
    relationship = models.CharField(max_length=16, choices=GuardianRelationship.choices)
    is_primary = models.BooleanField(default=False)
    is_emergency_contact = models.BooleanField(default=False)
    contact_preference = models.CharField(max_length=8, choices=ContactPreference.choices, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "people_student_guardian"
        constraints = [
            models.UniqueConstraint(fields=["student", "guardian"], name="people_student_guardian_uniq"),
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(is_primary=True),
                name="people_student_one_primary_guardian_uniq",
            ),
        ]
        indexes = [models.Index(fields=["school", "guardian"], name="people_student_guardian_idx")]

    def __str__(self) -> str:
        return str(self.id)


# ---------------------------------------------------------------------------------------------- enrollment
class EnrollmentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    WITHDRAWN = "withdrawn", "Withdrawn"
    TRANSFERRED = "transferred", "Transferred"


class Enrollment(TenantModel):
    """A student's place in a section for an academic year (ADR-007).

    ``academic_year`` and ``grade`` repeat the section's, and a composite foreign key to
    ``section (id, academic_year_id, grade_id, school_id)`` makes any mismatch impossible. Lifecycle:
    ``active`` -> ``completed`` | ``withdrawn`` | ``transferred`` (terminal). At most one active enrollment
    per student per academic year.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="enrollments")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="enrollments")
    grade = models.ForeignKey(Grade, on_delete=models.PROTECT, related_name="enrollments")
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="enrollments")
    roll_number = models.CharField(max_length=16, blank=True)
    status = models.CharField(
        max_length=16, choices=EnrollmentStatus.choices, default=EnrollmentStatus.ACTIVE
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    transferred_to = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="transferred_from"
    )
    status_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "people_enrollment"
        constraints = [
            models.UniqueConstraint(
                fields=["student", "academic_year"],
                condition=Q(status=EnrollmentStatus.ACTIVE),
                name="people_enrollment_one_active_per_year_uniq",
            ),
            models.UniqueConstraint(
                fields=["section", "roll_number"],
                condition=Q(status=EnrollmentStatus.ACTIVE) & ~Q(roll_number=""),
                name="people_enrollment_roll_number_uniq",
            ),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="people_enrollment_dates_check",
            ),
            models.CheckConstraint(
                condition=Q(status=EnrollmentStatus.ACTIVE, end_date__isnull=True)
                | (~Q(status=EnrollmentStatus.ACTIVE) & Q(end_date__isnull=False)),
                name="people_enrollment_end_date_matches_status_check",
            ),
            models.CheckConstraint(
                condition=Q(transferred_to__isnull=True) | Q(status=EnrollmentStatus.TRANSFERRED),
                name="people_enrollment_transfer_check",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "section", "status"], name="people_enrollment_section_idx"),
            models.Index(fields=["school", "student", "status"], name="people_enrollment_student_idx"),
        ]

    def __str__(self) -> str:
        return str(self.id)


# ---------------------------------------------------------------------------------------------- teaching
class AssignmentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ENDED = "ended", "Ended"


class TeacherAssignment(TenantModel):
    """A teacher teaching a subject to a section in a year, and/or being its class teacher (ADR-007).

    ``subject`` is empty only for a class-teacher (homeroom) assignment. One active class teacher per section.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    staff = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="assignments")
    academic_year = models.ForeignKey(
        AcademicYear, on_delete=models.PROTECT, related_name="teacher_assignments"
    )
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="teacher_assignments")
    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, null=True, blank=True, related_name="teacher_assignments"
    )
    is_class_teacher = models.BooleanField(default=False)
    status = models.CharField(max_length=8, choices=AssignmentStatus.choices, default=AssignmentStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "people_teacher_assignment"
        constraints = [
            models.CheckConstraint(
                condition=Q(subject__isnull=False) | Q(is_class_teacher=True),
                name="people_assignment_subject_or_class_teacher_check",
            ),
            models.UniqueConstraint(
                fields=["staff", "section", "subject"],
                condition=Q(status=AssignmentStatus.ACTIVE, subject__isnull=False),
                name="people_assignment_staff_section_subject_uniq",
            ),
            models.UniqueConstraint(
                fields=["section"],
                condition=Q(status=AssignmentStatus.ACTIVE, is_class_teacher=True),
                name="people_assignment_one_class_teacher_uniq",
            ),
            models.UniqueConstraint(
                fields=["staff", "section"],
                condition=Q(status=AssignmentStatus.ACTIVE, subject__isnull=True),
                name="people_assignment_staff_homeroom_uniq",
            ),
            # Target of the composite foreign key that keeps a timetable slot's teacher, section and subject
            # equal to its assignment's (timetable migration 0002).
            models.UniqueConstraint(
                fields=["id", "staff", "section", "subject", "school"], name="people_assignment_slot_key_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["school", "staff", "status"], name="people_assignment_staff_idx"),
            models.Index(fields=["school", "section", "status"], name="people_assignment_section_idx"),
        ]

    def __str__(self) -> str:
        return str(self.id)
