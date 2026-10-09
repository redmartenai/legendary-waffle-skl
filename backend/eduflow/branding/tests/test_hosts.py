"""Host resolution and binding: what a hostname resolves to; a host never authorises anything."""

import pytest

from eduflow.branding import hosts
from eduflow.branding.models import SchoolDomain
from eduflow.tenancy.models import School

pytestmark = pytest.mark.django_db

CUSTOM = "portal.greenvalley.test"


def _resolve(api_client, host):
    return api_client.get(f"/api/v1/branding/resolve?host={host}")


def _verified_domain(world, dns):
    from django.utils import timezone

    return SchoolDomain.objects.create(
        school=world.school,
        hostname=CUSTOM,
        status="verified",
        verified_at=timezone.now(),
        verification_token="t",
    )


# ------------------------------------------------------------------------------------------------ resolve
def test_the_platform_host_has_the_platform_branding(api_client):
    data = _resolve(api_client, "app.eduflow.test").json()
    assert (data["host_kind"], data["school"], data["display_name"]) == ("platform", None, "EduFlow")
    assert _resolve(api_client, "eduflow.test").json()["host_kind"] == "platform"


def test_a_school_subdomain_resolves_to_the_school(world, api_client):
    data = _resolve(api_client, f"{world.school.code}.eduflow.test").json()
    assert (data["host_kind"], data["school"]["id"]) == ("subdomain", str(world.school.pk))
    assert set(data["school"]) == {"id", "code", "name", "short_name"}  # public fields only
    assert _resolve(api_client, f"{world.school.code.upper()}.EDUFLOW.TEST:443").status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "www.eduflow.test",  # a reserved label
        "nope.eduflow.test",  # no such school
        "a.b.eduflow.test",  # nested label
        "unknown.example.org",  # not registered
        "evil.com",
        "127.0.0.1",
        "",
        "x" * 300,
    ],
)
def test_anything_else_is_404(world, api_client, host):
    response = (
        _resolve(api_client, host) if host else api_client.get("/api/v1/branding/resolve?host=bad_host!")
    )
    assert response.status_code in (400, 404)


def test_inactive_schools_do_not_resolve(world, api_client, dns):
    _verified_domain(world, dns)
    School.objects.filter(pk=world.school.pk).update(is_active=False)
    assert _resolve(api_client, f"{world.school.code}.eduflow.test").status_code == 404
    assert _resolve(api_client, CUSTOM).status_code == 404


def test_pending_and_disabled_domains_do_not_resolve(world, api_client):
    SchoolDomain.objects.create(school=world.school, hostname=CUSTOM, verification_token="t")
    assert _resolve(api_client, CUSTOM).status_code == 404
    SchoolDomain.objects.filter(hostname=CUSTOM).update(status="disabled")
    hosts.forget(CUSTOM)
    assert _resolve(api_client, CUSTOM).status_code == 404


def test_public_resolution_is_cacheable_and_rate_limited(world, api_client, settings):
    response = _resolve(api_client, f"{world.school.code}.eduflow.test")
    assert response["Cache-Control"] == "public, max-age=60"
    assert (
        _resolve(
            api_client,
            f"{world.school.code}.eduflow.test",
        ).status_code
        == 200
    )
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "branding_public_ip": "2/m"}
    from django.core.cache import cache

    cache.clear()
    codes = [_resolve(api_client, "app.eduflow.test").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_the_requests_own_host_is_used_when_no_host_is_given(world, api_client):
    response = api_client.get("/api/v1/branding/resolve", HTTP_HOST=f"{world.school.code}.eduflow.test")
    assert response.json()["school"]["id"] == str(world.school.pk)


def test_spoofed_hosts_never_reach_the_app(world, api_client):
    response = api_client.get("/api/v1/branding/resolve", HTTP_HOST="attacker.example")
    assert response.status_code == 400  # Django's ALLOWED_HOSTS check (DisallowedHost)


# ------------------------------------------------------------------------------------------------ binding
def test_a_school_host_selects_the_school_but_membership_still_decides(world, other_world, client_for):
    host = f"{world.school.code}.eduflow.test"
    member = client_for(world.parent.user)  # no X-School-Id: the host's school is used
    assert member.get("/api/v1/branding", HTTP_HOST=host).json()["school"]["id"] == str(world.school.pk)
    outsider = client_for(other_world.parent.user)
    response = outsider.get("/api/v1/branding", HTTP_HOST=host)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "tenant_forbidden")


def test_a_school_host_never_acts_for_another_school(world, other_world, client_for, make_member):
    # Someone who belongs to both schools, on school A's host, asks for school B.
    both = make_member(other_world.school, user=world.admin.user, roles=["school_admin"])
    client = client_for(both.user, other_world.school)
    response = client.get("/api/v1/branding", HTTP_HOST=f"{world.school.code}.eduflow.test")
    assert response.status_code == 403
    assert client.get("/api/v1/branding").json()["school"]["id"] == str(
        other_world.school.pk
    )  # platform host


def test_a_custom_domain_binds_like_a_subdomain(world, other_world, client_for, dns):
    _verified_domain(world, dns)
    assert client_for(world.teacher.user).get("/api/v1/branding", HTTP_HOST=CUSTOM).status_code == 200
    assert client_for(other_world.teacher.user).get("/api/v1/branding", HTTP_HOST=CUSTOM).status_code == 403


def test_platform_administration_is_refused_on_school_hosts(world, platform_admin):
    path = f"/api/v1/platform/schools/{world.school.pk}/branding"
    assert platform_admin.get(path).status_code == 200
    assert platform_admin.get(path, HTTP_HOST=f"{world.school.code}.eduflow.test").status_code == 403


def test_the_header_still_works_on_the_platform_host(world, client_for):
    client = client_for(world.teacher.user, world.school)
    assert client.get("/api/v1/branding", HTTP_HOST="app.eduflow.test").status_code == 200
    assert client_for(world.teacher.user).get("/api/v1/branding").json()["error"]["code"] == "tenant_required"


# ------------------------------------------------------------------------------------------------ integration
def test_the_public_school_lookup_includes_branding(world, as_member, api_client):
    as_member(world.admin).patch("/api/v1/branding", {"primary_color": "#ABCDEF"}, format="json")
    data = api_client.get(f"/api/v1/schools/lookup?code={world.school.code}").json()
    assert set(data) == {"id", "code", "name", "branding"}
    assert data["branding"]["primary_color"] == "#ABCDEF"
    assert "settings" not in data["branding"]
    assert "email" not in str(data)


def test_the_invitation_preview_shows_the_inviting_schools_branding(world, as_member, api_client):
    as_member(world.admin).patch("/api/v1/branding", {"primary_color": "#0A0A0A"}, format="json")
    from eduflow.identity.otp.providers import MemorySmsProvider

    created = as_member(world.admin).post(
        "/api/v1/invitations",
        {
            "kind": "student",
            "channel": "phone",
            "recipient": "+919811100001",
            "full_name": "Ravi",
            "student_id": str(world.other_student.pk),
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    token = MemorySmsProvider.outbox[-1].message.split("token=")[1].split()[0]
    preview = api_client.post("/api/v1/invitations/preview", {"token": token}, format="json").json()
    assert preview["branding"]["school"]["id"] == str(world.school.pk)
    assert preview["branding"]["primary_color"] == "#0A0A0A"
