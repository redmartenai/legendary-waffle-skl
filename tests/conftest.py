import io

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.core.tenant import unscoped, use_school
from apps.tenancy.models import School

PARENT_MEERA = "+919900000001"  # Aarav (8B) and Diya (6A), both on Route 4 at MG Road
PARENT_RAHUL = "+919900000002"  # Kabir (10C), Route 4 at Lake View
STUDENT_KABIR = "+919900000003"
TEACHER_ANITA = "+919800000002"  # class teacher 8B
TEACHER_VIKRAM = "+919800000003"  # class teacher 6A
PRINCIPAL = "+919800000001"
ACCOUNTANT = "+919800000005"
TRANSPORT_MANAGER = "+919800000006"
DRIVER = "+919800000007"
ATTENDANT = "+919800000008"
SPS_PARENT = "+919911100002"


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Load the demo schools once; each test runs in a transaction that is rolled back."""
    with django_db_blocker.unblock():
        call_command("seed_demo", "--reset", stdout=io.StringIO())


@pytest.fixture
def ghis(db):
    return School.objects.get(code="GHIS")


@pytest.fixture
def sps(db):
    return School.objects.get(code="SPS")


@pytest.fixture
def in_ghis(ghis):
    with use_school(ghis):
        yield ghis


def user(phone: str) -> User:
    with unscoped():
        return User.objects.get(phone=phone)


def api(phone: str, school: School | None = None) -> APIClient:
    client = APIClient()
    token = RefreshToken.for_user(user(phone)).access_token
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    if school is not None:
        headers["HTTP_X_SCHOOL_ID"] = str(school.id)
    client.credentials(**headers)
    return client


@pytest.fixture
def parent(ghis):
    return api(PARENT_MEERA, ghis)


@pytest.fixture
def teacher(ghis):
    return api(TEACHER_ANITA, ghis)


@pytest.fixture
def driver(ghis):
    return api(DRIVER, ghis)


@pytest.fixture
def principal(ghis):
    return api(PRINCIPAL, ghis)


@pytest.fixture
def transport_manager(ghis):
    return api(TRANSPORT_MANAGER, ghis)
