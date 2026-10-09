"""People serializers. Explicit fields only: no credentials, no OTP or token data, no authorization internals.

Nested data is one level of compact references (``id`` + names), never full related objects.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer

from ..models import (
    AssignmentStatus,
    ContactPreference,
    Enrollment,
    EnrollmentStatus,
    Gender,
    Guardian,
    GuardianRelationship,
    StaffProfile,
    StaffStatus,
    StaffType,
    Student,
    StudentGuardian,
    StudentStatus,
    TeacherAssignment,
)


class PersonRef(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    full_name = serializers.CharField()


# ------------------------------------------------------------------------------------------------ staff
class StaffOut(serializers.ModelSerializer[StaffProfile]):
    full_name = serializers.CharField(source="membership.user.full_name")
    department = Ref(allow_null=True)
    campus = Ref(allow_null=True)

    class Meta:
        model = StaffProfile
        fields = (
            "id",
            "membership_id",
            "full_name",
            "employee_id",
            "staff_type",
            "designation",
            "department",
            "campus",
            "joining_date",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class StaffCreateIn(StrictSerializer):
    membership_id = serializers.UUIDField(help_text="The person's active membership in this school.")
    employee_id = serializers.CharField(max_length=32)
    staff_type = serializers.ChoiceField(StaffType.choices, required=False)
    designation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    campus_id = serializers.UUIDField(required=False, allow_null=True)
    joining_date = serializers.DateField(required=False, allow_null=True)


class StaffUpdateIn(StrictSerializer):
    employee_id = serializers.CharField(max_length=32, required=False)
    staff_type = serializers.ChoiceField(StaffType.choices, required=False)
    designation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    campus_id = serializers.UUIDField(required=False, allow_null=True)
    joining_date = serializers.DateField(required=False, allow_null=True)
    status = serializers.ChoiceField(
        StaffStatus.choices,
        required=False,
        help_text="`left` also ends the person's active teacher assignments.",
    )


# ------------------------------------------------------------------------------------------------ students
class CurrentEnrollmentOut(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    academic_year = Ref()
    grade = Ref()
    section = Ref()
    roll_number = serializers.CharField()


class StudentOut(serializers.ModelSerializer[Student]):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Student
        fields = (
            "id",
            "membership_id",
            "admission_number",
            "first_name",
            "middle_name",
            "last_name",
            "full_name",
            "date_of_birth",
            "gender",
            "admission_date",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class StudentIn(StrictSerializer):
    admission_number = serializers.CharField(max_length=32)
    first_name = serializers.CharField(max_length=100)
    middle_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    gender = serializers.ChoiceField(Gender.choices, required=False, allow_blank=True)
    admission_date = serializers.DateField(required=False, allow_null=True)
    membership_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Only for students who sign in: their membership in this school.",
    )
    status = serializers.ChoiceField(StudentStatus.choices, required=False)


# ------------------------------------------------------------------------------------------------ guardians
class GuardianOut(serializers.ModelSerializer[Guardian]):
    class Meta:
        model = Guardian
        fields = (
            "id",
            "membership_id",
            "full_name",
            "phone",
            "email",
            "occupation",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class GuardianIn(StrictSerializer):
    full_name = serializers.CharField(max_length=200)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    occupation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    membership_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Only for guardians who sign in: their membership in this school.",
    )


class StudentGuardianOut(serializers.ModelSerializer[StudentGuardian]):
    student = PersonRef()
    guardian = PersonRef()

    class Meta:
        model = StudentGuardian
        fields = (
            "id",
            "student",
            "guardian",
            "relationship",
            "is_primary",
            "is_emergency_contact",
            "contact_preference",
            "created_at",
        )
        read_only_fields = fields


class StudentGuardianCreateIn(StrictSerializer):
    student_id = serializers.UUIDField()
    guardian_id = serializers.UUIDField()
    relationship = serializers.ChoiceField(GuardianRelationship.choices)
    is_primary = serializers.BooleanField(
        required=False, help_text="Making one primary un-sets the previous one."
    )
    is_emergency_contact = serializers.BooleanField(required=False)
    contact_preference = serializers.ChoiceField(ContactPreference.choices, required=False, allow_blank=True)


class StudentGuardianUpdateIn(StrictSerializer):
    relationship = serializers.ChoiceField(GuardianRelationship.choices, required=False)
    is_primary = serializers.BooleanField(required=False)
    is_emergency_contact = serializers.BooleanField(required=False)
    contact_preference = serializers.ChoiceField(ContactPreference.choices, required=False, allow_blank=True)


# ------------------------------------------------------------------------------------------------ enrollment
class EnrollmentOut(serializers.ModelSerializer[Enrollment]):
    student = PersonRef()
    academic_year = Ref()
    grade = Ref()
    section = Ref()

    class Meta:
        model = Enrollment
        fields = (
            "id",
            "student",
            "academic_year",
            "grade",
            "section",
            "roll_number",
            "status",
            "start_date",
            "end_date",
            "status_reason",
            "transferred_to_id",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class EnrollmentCreateIn(StrictSerializer):
    student_id = serializers.UUIDField()
    section_id = serializers.UUIDField(help_text="The academic year and grade are taken from the section.")
    roll_number = serializers.CharField(max_length=16, required=False, allow_blank=True)
    start_date = serializers.DateField(required=False, help_text="Defaults to today.")


class EnrollmentUpdateIn(StrictSerializer):
    roll_number = serializers.CharField(max_length=16, required=False, allow_blank=True)


class EnrollmentEndIn(StrictSerializer):
    status = serializers.ChoiceField([EnrollmentStatus.COMPLETED, EnrollmentStatus.WITHDRAWN])
    end_date = serializers.DateField(required=False, help_text="Defaults to today.")
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)


class EnrollmentTransferIn(StrictSerializer):
    section_id = serializers.UUIDField(help_text="A section of the same academic year.")
    date = serializers.DateField(required=False, help_text="Defaults to today.")
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)
    roll_number = serializers.CharField(max_length=16, required=False, allow_blank=True)


# ------------------------------------------------------------------------------------------------ assignments
class TeacherAssignmentOut(serializers.ModelSerializer[TeacherAssignment]):
    staff = serializers.SerializerMethodField()
    academic_year = Ref()
    section = Ref()
    subject = Ref(allow_null=True)

    class Meta:
        model = TeacherAssignment
        fields = (
            "id",
            "staff",
            "academic_year",
            "section",
            "subject",
            "is_class_teacher",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_staff(self, obj: TeacherAssignment) -> dict[str, Any]:
        return {"id": obj.staff_id, "full_name": obj.staff.membership.user.full_name}


class TeacherAssignmentCreateIn(StrictSerializer):
    staff_id = serializers.UUIDField()
    section_id = serializers.UUIDField(help_text="The academic year is taken from the section.")
    subject_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="Empty only for a class teacher."
    )
    is_class_teacher = serializers.BooleanField(required=False, default=False)


class TeacherAssignmentUpdateIn(StrictSerializer):
    is_class_teacher = serializers.BooleanField(required=False)
    status = serializers.ChoiceField(AssignmentStatus.choices, required=False)
