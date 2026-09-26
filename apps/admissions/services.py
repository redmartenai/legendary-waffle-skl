"""Admissions: turning an admitted application into a Student."""

from datetime import date

from django.db.models import Max

from apps.academics.models import ClassGroup, Student, StudentGuardian
from apps.accounts.models import Membership, Role, User
from apps.core.utils import normalize_phone


def next_admission_no(year: int) -> str:
    """SPS/<year>/<n>: the running number continues across years."""
    last = max((int(n.rsplit("/", 1)[1]) for n in Student.objects.values_list("admission_no", flat=True) if n.rsplit("/", 1)[-1].isdigit()), default=0)
    prefix = (Student.objects.values_list("admission_no", flat=True).first() or "SPS/").split("/")[0]
    return f"{prefix}/{year}/{last + 1:04d}"


def link_guardian(student, school, name: str, phone: str, relationship: str = "parent"):
    phone = normalize_phone(phone)
    user = User.objects.filter(phone=phone).first() or User.objects.create_user(phone, name or "Parent")
    Membership.objects.get_or_create(user=user, role=Role.PARENT, school=school, defaults={"title": "Parent"})
    link, _ = StudentGuardian.objects.get_or_create(
        student=student, user=user, defaults={"relationship": relationship or "parent", "is_primary": not StudentGuardian.objects.filter(student=student).exists()}
    )
    return user


def create_admitted_student(app, school, class_group=None) -> Student:
    """The Student record for an admitted child (inactive until the new session starts) with the family linked."""
    group = class_group or ClassGroup.objects.filter(grade=app.grade, academic_year__is_current=True).order_by("section").first()
    if group is None:
        group = ClassGroup.objects.filter(academic_year__is_current=True).order_by("grade", "section").first()
    year = int(app.academic_year[:4]) if app.academic_year[:4].isdigit() else date.today().year
    roll = (Student.objects.filter(class_group=group).aggregate(m=Max("roll_no"))["m"] or 0) + 1
    student = Student.objects.create(full_name=app.child_name, admission_no=next_admission_no(year), roll_no=roll, class_group=group, is_active=False)
    if app.guardian_phone:
        link_guardian(student, school, app.guardian_name, app.guardian_phone)
    app.student = student
    app.save(update_fields=["student", "updated_at"])
    return student
