"""Platform admin: register schools, hand over credentials, invites, forced password change, pausing."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.academics.models import AcademicYear, ClassGroup
from apps.accounts.models import Membership, User
from apps.core.tenant import unscoped
from apps.platform.models import CredentialIssue, SchoolInvite
from apps.tenancy.models import School

from .conftest import PRINCIPAL, api, user

PLATFORM = "+919000000000"  # the seed's EduFlow staff account (is_staff)


def _payload(code="VALLEY", principal_phone="+919876500001", **extra):
    return {
        "school": {"name": "Test Valley School", "code": code, "city": "Mysuru", "state": "Karnataka", "primary_color": "#2E6A4F"},
        "year": {
            "name": "2026–27",
            "starts_on": "2026-04-01",
            "ends_on": "2027-03-31",
            "terms": [{"name": "Term 1", "starts_on": "2026-04-01", "ends_on": "2026-09-30"}, {"name": "Term 2", "starts_on": "2026-10-01", "ends_on": "2027-03-31"}],
        },
        "grades": [{"grade": "1", "sections": ["A", "B"]}, {"grade": "2", "sections": ["A"]}],
        "principal": {"name": "Meena Iyer", "phone": principal_phone, "email": "meena@valley.test"},
        **extra,
    }


def _sign_in(identifier, password, code=""):
    return APIClient().post("/api/v1/auth/password/login", {"identifier": identifier, "password": password, "school_code": code}, format="json")


@pytest.mark.django_db
def test_only_platform_staff_can_use_platform(ghis):
    assert api(PRINCIPAL, ghis).get("/api/v1/platform/overview").status_code == 403
    assert APIClient().get("/api/v1/platform/overview").status_code == 401
    assert api(PLATFORM).get("/api/v1/platform/overview").status_code == 200


@pytest.mark.django_db
def test_register_school_creates_everything_and_a_one_time_slip(db):
    res = api(PLATFORM).post("/api/v1/platform/schools", _payload(), format="json")
    assert res.status_code == 201, res.json()
    body = res.json()
    slip = body["slips"][0]
    assert slip["method"] == "password" and len(slip["password"]) == 12 and "school=VALLEY" in slip["sign_in_url"]
    with unscoped():
        school = School.objects.get(code="VALLEY")
        assert AcademicYear.all_objects.filter(school=school, is_current=True).count() == 1
        assert ClassGroup.all_objects.filter(school=school).count() == 3
        principal = User.objects.get(phone="+919876500001")
        assert Membership.all_objects.filter(school=school, user=principal, role="principal").exists()
        assert principal.must_change_password and principal.check_password(slip["password"])
        assert CredentialIssue.objects.filter(school=school, method="password").count() == 1
    assert body["school"]["sections"] == 3


@pytest.mark.django_db
def test_duplicate_code_leaves_nothing_behind(ghis):
    res = api(PLATFORM).post("/api/v1/platform/schools", _payload(code="GHIS", principal_phone="+919876500002"), format="json")
    assert res.status_code == 400 and "school.code" in res.json()["error"]["fields"]
    # Bad grades fail after the school row would have been created: the transaction must roll it back.
    bad = _payload(code="EMPTY1", principal_phone="+919876500003")
    bad["grades"] = [{"grade": "1", "sections": []}]
    assert api(PLATFORM).post("/api/v1/platform/schools", bad, format="json").status_code == 400
    with unscoped():
        assert not School.objects.filter(code="EMPTY1").exists()
        assert not User.objects.filter(phone="+919876500003").exists()


@pytest.mark.django_db
def test_temporary_password_forces_a_change(db):
    slip = api(PLATFORM).post("/api/v1/platform/schools", _payload(code="FORCE"), format="json").json()["slips"][0]
    session = _sign_in("+919876500001", slip["password"], "FORCE").json()
    assert session["user"]["must_change_password"] is True
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session['access']}")
    with unscoped():
        school = School.objects.get(code="FORCE")
    blocked = client.get("/api/v1/console/context", HTTP_X_SCHOOL_ID=str(school.id))
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "password_change_required"
    assert client.post("/api/v1/auth/password/change", {"current": slip["password"], "new": slip["password"]}, format="json").status_code == 400
    assert client.post("/api/v1/auth/password/change", {"current": slip["password"], "new": "valley-principal-2026"}, format="json").status_code == 200
    assert client.get("/api/v1/console/context", HTTP_X_SCHOOL_ID=str(school.id)).status_code == 200
    assert _sign_in("+919876500001", slip["password"], "FORCE").status_code == 400


@pytest.mark.django_db
def test_invite_link_is_single_use_hashed_and_expires(db):
    body = api(PLATFORM).post("/api/v1/platform/schools", {**_payload(code="INVITE"), "method": "invite"}, format="json").json()
    slip = body["slips"][0]
    token = slip["invite_url"].rsplit("/", 1)[-1]
    assert slip["password"] is None
    assert not SchoolInvite.objects.filter(token_hash=token).exists()  # only the hash is stored
    public = APIClient()
    assert public.get(f"/api/v1/auth/invite/{token}").json()["school"]["code"] == "INVITE"
    assert public.post(f"/api/v1/auth/invite/{token}", {"password": "short"}, format="json").status_code == 400
    session = public.post(f"/api/v1/auth/invite/{token}", {"password": "valley-invite-2026"}, format="json")
    assert session.status_code == 200 and session.json()["memberships"][0]["school"]["code"] == "INVITE"
    assert public.post(f"/api/v1/auth/invite/{token}", {"password": "valley-invite-2027"}, format="json").status_code == 400
    # A second invite that has expired is refused too.
    school_id = body["school"]["id"]
    person_id = slip["person"]["id"]
    again = api(PLATFORM).post(f"/api/v1/platform/schools/{school_id}/people/{person_id}/credentials", {"method": "invite"}, format="json").json()["slip"]
    token2 = again["invite_url"].rsplit("/", 1)[-1]
    SchoolInvite.objects.filter(revoked_at__isnull=True, used_at__isnull=True).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert public.get(f"/api/v1/auth/invite/{token2}").status_code == 400


@pytest.mark.django_db
def test_reset_replaces_password_and_revokes_invites(db):
    body = api(PLATFORM).post("/api/v1/platform/schools", {**_payload(code="RESET"), "method": "invite"}, format="json").json()
    old = body["slips"][0]["invite_url"].rsplit("/", 1)[-1]
    url = f"/api/v1/platform/schools/{body['school']['id']}/people/{body['slips'][0]['person']['id']}/credentials"
    fresh = api(PLATFORM).post(url, {"method": "password", "send": ["sms", "email"]}, format="json").json()["slip"]
    assert fresh["delivered"] == {"sms": "logged", "email": "logged"}
    assert APIClient().get(f"/api/v1/auth/invite/{old}").status_code == 400
    assert _sign_in("+919876500001", fresh["password"], "RESET").status_code == 200


@pytest.mark.django_db
def test_paused_school_cannot_be_signed_into(db):
    body = api(PLATFORM).post("/api/v1/platform/schools", _payload(code="PAUSE"), format="json").json()
    password = body["slips"][0]["password"]
    assert api(PLATFORM).patch(f"/api/v1/platform/schools/{body['school']['id']}", {"is_active": False}, format="json").json()["is_active"] is False
    assert _sign_in("+919876500001", password, "PAUSE").status_code == 404


@pytest.mark.django_db
def test_existing_user_keeps_password_and_gets_no_slip(ghis):
    existing = user(PRINCIPAL)
    body = api(PLATFORM).post("/api/v1/platform/schools", _payload(code="SECOND", principal_phone=PRINCIPAL), format="json").json()
    slip = body["slips"][0]
    assert slip["method"] == "existing" and slip["password"] is None
    existing.refresh_from_db()
    assert existing.must_change_password is False
    with unscoped():
        # Still a principal at their first school, and now at the new one.
        assert Membership.all_objects.filter(user=existing, role="principal").count() == 2


@pytest.mark.django_db
def test_platform_staff_sign_in_without_a_school(db):
    with unscoped():
        staff = User.objects.get(phone=PLATFORM)
    staff.set_password("platform-pass-2026")
    staff.save()
    session = _sign_in(PLATFORM, "platform-pass-2026").json()
    assert session["user"]["platform"] is True


@pytest.mark.django_db
def test_check_code_suggests_and_validates(ghis):
    res = api(PLATFORM).post("/api/v1/platform/schools/check-code", {"code": "ghis", "name": "Green Hills International School"}, format="json").json()
    assert res["valid"] and not res["available"] and res["suggestion"] and res["suggestion"] != "GHIS"
