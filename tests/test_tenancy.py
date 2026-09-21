import pytest

from apps.academics.models import Student
from apps.core.tenant import TenantContextMissing, unscoped, use_school

from .conftest import PARENT_MEERA, SPS_PARENT, api


@pytest.mark.django_db
def test_scoped_query_without_school_fails_closed():
    with pytest.raises(TenantContextMissing):
        list(Student.objects.all())


@pytest.mark.django_db
def test_scoped_query_only_sees_active_school(ghis, sps):
    with use_school(ghis):
        names = set(Student.objects.values_list("full_name", flat=True))
    with use_school(sps):
        other = set(Student.objects.values_list("full_name", flat=True))
    assert "Aarav Iyer" in names and "Ishita Joshi" not in names
    assert other == {"Ishita Joshi"}


@pytest.mark.django_db
def test_cross_school_write_is_blocked(ghis, sps):
    with unscoped():
        ishita = Student.all_objects.get(full_name="Ishita Joshi")
    with use_school(ghis):
        ishita.full_name = "Changed"
        with pytest.raises(TenantContextMissing):
            ishita.save()


@pytest.mark.django_db
def test_user_cannot_use_a_school_they_do_not_belong_to(sps):
    response = api(PARENT_MEERA, sps).get("/api/v1/parent/children")
    assert response.status_code == 403


@pytest.mark.django_db
def test_parent_of_other_school_cannot_see_greenwood_student(ghis, sps):
    with use_school(ghis):
        aarav = Student.objects.get(full_name="Aarav Iyer")
    response = api(SPS_PARENT, sps).get(f"/api/v1/students/{aarav.id}/summary")
    assert response.status_code == 404


@pytest.mark.django_db
def test_offboarding_a_school_removes_only_its_data(ghis, sps):
    with unscoped():
        greenwood_students = Student.all_objects.filter(school=ghis).count()
        sps.delete()  # a school leaving the platform
        assert not Student.all_objects.filter(full_name="Ishita Joshi").exists()
        assert Student.all_objects.filter(school=ghis).count() == greenwood_students


@pytest.mark.django_db
def test_a_class_with_students_cannot_be_deleted_on_its_own(in_ghis):
    from django.db.models import RestrictedError

    from apps.academics.models import ClassGroup

    with pytest.raises(RestrictedError):
        ClassGroup.objects.get(grade="8", section="B").delete()
