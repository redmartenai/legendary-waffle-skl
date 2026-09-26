"""Who may see which student. Every student-facing endpoint goes through here."""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F, Q
from django.http import Http404

from apps.accounts.models import MANAGEMENT_ROLES, Role

from .models import ClassGroup, Student, StudentGuardian, TeachingAssignment


def children_of(user):
    """Active students linked to this guardian in the current school, eldest first."""
    return (
        Student.objects.filter(is_active=True, guardian_links__user=user)
        .select_related("class_group")
        .distinct()
        .order_by(F("date_of_birth").asc(nulls_last=True), "full_name")
    )


def teacher_class_ids(user) -> set:
    taught = set(TeachingAssignment.objects.filter(teacher=user).values_list("class_group_id", flat=True))
    led = set(ClassGroup.objects.filter(class_teacher=user).values_list("id", flat=True))
    return taught | led


def student_for_request(request, student_id, *, purpose: str = "view") -> Student:
    """Return the student if the caller may see them, otherwise 404 (never 403, to avoid probing)."""
    try:
        student = Student.objects.select_related("class_group").get(id=student_id, is_active=True)
    except (Student.DoesNotExist, ValueError, DjangoValidationError) as exc:
        raise Http404 from exc

    roles = request.roles
    user = request.user
    if roles & MANAGEMENT_ROLES:
        return student
    if Role.ACCOUNTANT in roles and purpose in {"view", "fees"}:
        return student
    if Role.TRANSPORT_MANAGER in roles and purpose in {"view", "transport"}:
        return student
    if Role.PARENT in roles and StudentGuardian.objects.filter(student=student, user=user).exists():
        return student
    if Role.STUDENT in roles and student.user_id == user.id:
        return student
    if Role.TEACHER in roles and student.class_group_id in teacher_class_ids(user):
        return student
    raise Http404


def can_manage_class(request, class_group) -> bool:
    if request.roles & MANAGEMENT_ROLES:
        return True
    return Role.TEACHER in request.roles and class_group.id in teacher_class_ids(request.user)


def guardians_of(students, *, alerts_only: bool = True) -> dict:
    """Map guardian user -> list of their students (one entry per person, siblings merged)."""
    links = StudentGuardian.objects.filter(student__in=students).select_related("user", "student")
    if alerts_only:
        links = links.filter(receives_alerts=True)
    result: dict = {}
    for link in links:
        if link.user.is_active:
            result.setdefault(link.user, []).append(link.student)
    return result


def search_filter(term: str) -> Q:
    return Q(full_name__icontains=term) | Q(admission_no__icontains=term)
