from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer

from ..models import (
    AcademicYear,
    AcademicYearStatus,
    Campus,
    Department,
    Grade,
    RecordStatus,
    Section,
    Subject,
    SubjectCategory,
)

ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "postal_code", "country")


class Ref(serializers.Serializer[Any]):
    """A compact reference to a related record (no nesting beyond one level)."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    code = serializers.CharField(required=False)


# ------------------------------------------------------------------------------------------------ campus
class CampusOut(serializers.ModelSerializer[Campus]):
    class Meta:
        model = Campus
        fields = ("id", "name", "code", *ADDRESS_FIELDS, "status", "created_at", "updated_at")
        read_only_fields = fields


class CampusIn(StrictSerializer):
    name = serializers.CharField(max_length=150)
    code = serializers.SlugField(max_length=32)
    address_line1 = serializers.CharField(max_length=200, required=False, allow_blank=True)
    address_line2 = serializers.CharField(max_length=200, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    postal_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    country = serializers.RegexField(r"^[A-Z]{2}$", required=False)
    status = serializers.ChoiceField(RecordStatus.choices, required=False)


# ------------------------------------------------------------------------------------------------ year
class AcademicYearOut(serializers.ModelSerializer[AcademicYear]):
    class Meta:
        model = AcademicYear
        fields = ("id", "name", "start_date", "end_date", "status", "is_current", "created_at", "updated_at")
        read_only_fields = fields


class AcademicYearCreateIn(StrictSerializer):
    name = serializers.CharField(max_length=50)
    start_date = serializers.DateField()
    end_date = serializers.DateField()


class AcademicYearUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=50, required=False)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    status = serializers.ChoiceField(
        AcademicYearStatus.choices, required=False, help_text="planned -> active -> closed. Closed is final."
    )
    is_current = serializers.BooleanField(
        required=False, help_text="Making a year current un-sets the previous one."
    )


# ------------------------------------------------------------------------------------------------ department
class DepartmentOut(serializers.ModelSerializer[Department]):
    class Meta:
        model = Department
        fields = ("id", "name", "code", "description", "status", "created_at", "updated_at")
        read_only_fields = fields


class DepartmentIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    code = serializers.SlugField(max_length=32)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    status = serializers.ChoiceField(RecordStatus.choices, required=False)


# ------------------------------------------------------------------------------------------------ grade
class GradeOut(serializers.ModelSerializer[Grade]):
    class Meta:
        model = Grade
        fields = ("id", "name", "code", "display_order", "status", "created_at", "updated_at")
        read_only_fields = fields


class GradeIn(StrictSerializer):
    name = serializers.CharField(max_length=50)
    code = serializers.SlugField(max_length=32)
    display_order = serializers.IntegerField(min_value=0, max_value=32767, required=False)
    status = serializers.ChoiceField(RecordStatus.choices, required=False)


# ------------------------------------------------------------------------------------------------ section
class SectionOut(serializers.ModelSerializer[Section]):
    academic_year = Ref()
    grade = Ref()
    campus = Ref(allow_null=True)

    class Meta:
        model = Section
        fields = (
            "id",
            "name",
            "code",
            "academic_year",
            "grade",
            "campus",
            "capacity",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class SectionCreateIn(StrictSerializer):
    academic_year_id = serializers.UUIDField()
    grade_id = serializers.UUIDField()
    campus_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=50)
    code = serializers.SlugField(max_length=32)
    capacity = serializers.IntegerField(min_value=1, max_value=32767, required=False, allow_null=True)


class SectionUpdateIn(StrictSerializer):
    """The year and grade of a section are fixed; create a new section instead."""

    campus_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=50, required=False)
    code = serializers.SlugField(max_length=32, required=False)
    capacity = serializers.IntegerField(min_value=1, max_value=32767, required=False, allow_null=True)
    status = serializers.ChoiceField(RecordStatus.choices, required=False)


# ------------------------------------------------------------------------------------------------ subject
class SubjectOut(serializers.ModelSerializer[Subject]):
    department = Ref(allow_null=True)

    class Meta:
        model = Subject
        fields = (
            "id",
            "name",
            "code",
            "short_name",
            "description",
            "category",
            "department",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class SubjectIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    code = serializers.SlugField(max_length=32)
    short_name = serializers.CharField(max_length=20, required=False, allow_blank=True)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    category = serializers.ChoiceField(SubjectCategory.choices, required=False)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    status = serializers.ChoiceField(RecordStatus.choices, required=False)
