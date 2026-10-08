from django.conf import settings
from django.db import models

from eduflow.tenancy.models import TenantModel


class Section(TenantModel):
    name = models.CharField(max_length=50)
    department = models.CharField(max_length=50, blank=True)


class Student(TenantModel):
    name = models.CharField(max_length=100)
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="students")
    # The student's own login, for "self" scope.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )


class TeacherSection(TenantModel):
    membership = models.ForeignKey("tenancy.Membership", on_delete=models.CASCADE, related_name="+")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="teachers")


class Mentorship(TenantModel):
    """A student individually assigned to a member (e.g. a mentor or a bus driver): "assigned" scope."""

    membership = models.ForeignKey("tenancy.Membership", on_delete=models.CASCADE, related_name="+")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="mentors")


class Guardianship(TenantModel):
    membership = models.ForeignKey("tenancy.Membership", on_delete=models.CASCADE, related_name="+")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="guardians")
