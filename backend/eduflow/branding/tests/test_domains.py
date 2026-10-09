"""Custom domains: registration, DNS proof, lifecycle, platform control, recheck, RLS and permissions."""

import pytest
from django.db import connection

from eduflow.audit.models import AuditEvent
from eduflow.branding import services
from eduflow.branding.models import DomainStatus, SchoolDomain
from eduflow.core import db_context
from eduflow.core.db_context import DbContext

pytestmark = pytest.mark.django_db

HOST = "portal.greenvalley.test"


def _add(client, hostname=HOST, expect=201):
    response = client.post("/api/v1/domains", {"hostname": hostname}, format="json")
    assert response.status_code == expect, response.content
    return response.json()


def _resolve(api_client, host):
    return api_client.get(f"/api/v1/branding/resolve?host={host}")


# ------------------------------------------------------------------------------------------------ register
def test_a_school_registers_a_domain_and_gets_its_dns_challenge(world, as_member):
    domain = _add(as_member(world.admin), "Portal.GreenValley.TEST.")
    assert (domain["hostname"], domain["status"], domain["is_primary"]) == (HOST, "pending", False)
    record = domain["verification"]
    assert record["type"] == "TXT"
    assert record["name"] == f"_eduflow-challenge.{HOST}"
    assert record["value"].startswith("eduflow-domain-verification=")
    assert AuditEvent.objects.filter(action="branding.domain.added", target_id=domain["id"]).exists()


def test_internationalised_names_are_stored_in_ascii(world, as_member):
    assert _add(as_member(world.admin), "schüle.example")["hostname"] == "xn--schle-mva.example"


@pytest.mark.parametrize(
    "hostname",
    [
        "localhost",
        "192.168.1.10",
        "[::1]",
        "*.example.com",
        "portal.example.com:8443",
        "https://portal.example.com",
        "-bad-.example.com",
        "a" * 64 + ".example.com",
        "example.123",
        "app.eduflow.test",  # a platform host
        "eduflow.test",  # the base domain
        "anything.eduflow.test",  # the platform's subdomain namespace
    ],
)
def test_invalid_and_platform_domains_are_refused(world, as_member, hostname):
    assert "hostname" in _add(as_member(world.admin), hostname, expect=400)["error"]["fields"]


def test_a_domain_belongs_to_one_school_only(world, other_world, as_member):
    _add(as_member(world.admin))
    taken = _add(as_member(other_world.admin), HOST.upper(), expect=409)
    assert taken["error"]["code"] == "conflict"
    _add(as_member(world.admin), expect=409)


# ------------------------------------------------------------------------------------------------ verify
def test_dns_proof_verifies_and_the_domain_then_resolves(world, as_member, api_client, dns):
    admin = as_member(world.admin)
    domain = _add(admin)
    assert _resolve(api_client, HOST).status_code == 404  # pending: never resolves
    missing = admin.post(f"/api/v1/domains/{domain['id']}/verify")
    assert missing.status_code == 400
    assert AuditEvent.objects.filter(action="branding.domain.verification_failed").exists()
    dns(HOST, "a-wrong-token")
    assert admin.post(f"/api/v1/domains/{domain['id']}/verify").status_code == 400
    dns(HOST, SchoolDomain.objects.get().verification_token)
    verified = admin.post(f"/api/v1/domains/{domain['id']}/verify").json()
    assert (verified["status"], verified["verification"]) == ("verified", None)
    resolved = _resolve(api_client, HOST).json()
    assert (resolved["host_kind"], resolved["school"]["id"]) == ("custom", str(world.school.pk))


def test_without_a_dns_verifier_the_check_answers_503(world, as_member, settings):
    settings.DOMAIN_VERIFIER = "eduflow.branding.verification.DisabledVerifier"
    domain = _add(as_member(world.admin))
    response = as_member(world.admin).post(f"/api/v1/domains/{domain['id']}/verify")
    assert response.status_code == 503
    assert SchoolDomain.objects.get().status == "pending"


def test_disabling_stops_resolution_at_once(world, as_member, api_client, dns):
    admin = as_member(world.admin)
    domain = _add(admin)
    dns(HOST, SchoolDomain.objects.get().verification_token)
    admin.post(f"/api/v1/domains/{domain['id']}/verify")
    assert _resolve(api_client, HOST).status_code == 200  # now cached
    assert admin.post(f"/api/v1/domains/{domain['id']}/disable").json()["status"] == "disabled"
    assert _resolve(api_client, HOST).status_code == 404  # the cache entry was dropped
    assert admin.post(f"/api/v1/domains/{domain['id']}/verify").json()["status"] == "verified"  # re-enable


def test_only_a_verified_domain_can_be_primary(world, as_member, dns):
    admin = as_member(world.admin)
    first, second = _add(admin), _add(admin, "www.greenvalley.test")
    assert admin.post(f"/api/v1/domains/{first['id']}/primary").status_code == 409
    for d in SchoolDomain.objects.all():
        dns(d.hostname, d.verification_token)
    admin.post(f"/api/v1/domains/{first['id']}/verify")
    admin.post(f"/api/v1/domains/{second['id']}/verify")
    admin.post(f"/api/v1/domains/{first['id']}/primary")
    admin.post(f"/api/v1/domains/{second['id']}/primary")
    assert list(SchoolDomain.objects.filter(is_primary=True).values_list("hostname", flat=True)) == [
        "www.greenvalley.test"
    ]


def test_removing_a_domain(world, as_member, api_client, dns):
    admin = as_member(world.admin)
    domain = _add(admin)
    dns(HOST, SchoolDomain.objects.get().verification_token)
    admin.post(f"/api/v1/domains/{domain['id']}/verify")
    assert admin.delete(f"/api/v1/domains/{domain['id']}").status_code == 204
    assert _resolve(api_client, HOST).status_code == 404
    assert AuditEvent.objects.filter(action="branding.domain.removed").exists()


# ------------------------------------------------------------------------------------------------ platform
def test_a_platform_suspension_binds_the_school(world, as_member, platform_admin, api_client, dns):
    admin = as_member(world.admin)
    domain = _add(admin)
    dns(HOST, SchoolDomain.objects.get().verification_token)
    admin.post(f"/api/v1/domains/{domain['id']}/verify")
    suspended = platform_admin.post(
        f"/api/v1/platform/domains/{domain['id']}/suspend", {"reason": "abuse_report"}, format="json"
    ).json()
    assert (suspended["status"], suspended["suspended_by_platform"]) == ("disabled", True)
    assert _resolve(api_client, HOST).status_code == 404
    for action in ("verify", "disable"):
        assert admin.post(f"/api/v1/domains/{domain['id']}/{action}").status_code == 409
    assert admin.delete(f"/api/v1/domains/{domain['id']}").status_code == 409  # else re-adding would lift it
    lifted = platform_admin.post(
        f"/api/v1/platform/domains/{domain['id']}/verify", {"note": "Owner confirmed by phone"}, format="json"
    ).json()
    assert (lifted["status"], lifted["suspended_by_platform"]) == ("verified", False)
    event = AuditEvent.objects.filter(action="branding.domain.verified").latest("occurred_at")
    assert (event.metadata["method"], event.metadata["by_platform"]) == ("manual", True)
    assert event.school_id == world.school.pk


def test_platform_manages_any_schools_branding_and_domains(world, platform_admin):
    path = f"/api/v1/platform/schools/{world.school.pk}"
    assert (
        platform_admin.patch(f"{path}/branding", {"primary_color": "#333333"}, format="json").json()[
            "primary_color"
        ]
        == "#333333"
    )
    created = platform_admin.post(f"{path}/domains", {"hostname": HOST}, format="json")
    assert created.status_code == 201
    assert [d["hostname"] for d in platform_admin.get(f"{path}/domains").json()] == [HOST]
    assert platform_admin.delete(f"/api/v1/platform/domains/{created.json()['id']}").status_code == 204


def test_platform_endpoints_need_a_platform_administrator(world, as_member):
    admin = as_member(world.admin)
    path = f"/api/v1/platform/schools/{world.school.pk}"
    assert admin.get(f"{path}/branding").status_code == 403
    assert admin.post(f"{path}/domains", {"hostname": HOST}, format="json").status_code == 403


def test_platform_administrators_have_no_implicit_school_access(world, make_user, client_for):
    staff = make_user(is_platform_admin=True)
    response = client_for(staff, world.school).get("/api/v1/domains")
    assert response.json()["error"]["code"] == "tenant_forbidden"


# ------------------------------------------------------------------------------------------------ recheck
def test_a_lapsed_proof_disables_the_domain_after_three_checks(world, as_member, api_client, dns):
    from eduflow.branding.verification import MemoryVerifier

    domain = _add(as_member(world.admin))
    dns(HOST, SchoolDomain.objects.get().verification_token)
    as_member(world.admin).post(f"/api/v1/domains/{domain['id']}/verify")
    assert services.recheck_verified_domains() == {"checked": 1, "lapsed": 0}
    MemoryVerifier.records.clear()  # the owner removed the TXT record (or lost the domain)
    for _ in range(2):
        services.recheck_verified_domains()
    assert SchoolDomain.objects.get().status == "verified"  # two misses are tolerated
    assert services.recheck_verified_domains() == {"checked": 1, "lapsed": 1}
    lapsed = SchoolDomain.objects.get()
    assert (lapsed.status, lapsed.disabled_reason) == ("disabled", "verification_lapsed")
    assert _resolve(api_client, HOST).status_code == 404
    assert AuditEvent.objects.filter(action="branding.domain.lapsed").exists()


def test_the_recheck_does_nothing_without_a_verifier(world, settings):
    settings.DOMAIN_VERIFIER = "eduflow.branding.verification.DisabledVerifier"
    assert services.recheck_verified_domains() == {"checked": 0, "lapsed": 0}


def test_the_recheck_runs_as_a_celery_task(world):
    from eduflow.branding.tasks import recheck_domains

    assert recheck_domains.delay().get() == {"checked": 0, "lapsed": 0}


# ------------------------------------------------------------------------------------------------ access
@pytest.mark.parametrize("role", ["teacher", "parent", "student_member"])
def test_only_domain_managers_see_domains(world, as_member, role):
    client = as_member(getattr(world, role))
    assert client.get("/api/v1/domains").status_code == 403
    assert client.post("/api/v1/domains", {"hostname": HOST}, format="json").status_code == 403


def test_another_schools_domain_answers_like_an_unknown_one(world, other_world, as_member):
    import uuid

    foreign = _add(as_member(other_world.admin))
    admin = as_member(world.admin)
    for suffix in ("", "/verify", "/disable", "/primary"):
        method = admin.get if suffix == "" else admin.post
        assert method(f"/api/v1/domains/{foreign['id']}{suffix}").status_code == 404
        assert method(f"/api/v1/domains/{uuid.uuid4()}{suffix}").status_code == 404
    assert admin.delete(f"/api/v1/domains/{foreign['id']}").status_code == 404
    assert SchoolDomain.objects.get(pk=foreign["id"]).status == "pending"
    assert admin.get("/api/v1/domains").json() == []


def test_branding_tables_are_under_rls(world, other_world, as_member):
    _add(as_member(other_world.admin))
    as_member(other_world.admin).patch("/api/v1/branding", {"primary_color": "#123123"}, format="json")
    from eduflow.branding.models import SchoolBranding

    with db_context.scoped(DbContext()):
        assert not SchoolDomain.objects.exists()
        assert not SchoolBranding.objects.exists()
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert SchoolDomain.objects.filter(hostname=HOST).update(status=DomainStatus.VERIFIED) == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename = ANY(%s)",
            [["branding_asset", "branding_school_branding", "branding_domain"]],
        )
        assert cursor.fetchone()[0] == 3


# ------------------------------------------------------------------------------------------------ isolation
BRANDING_MATRIX = [
    ("get", "/api/v1/domains/{domain}"),
    ("delete", "/api/v1/domains/{domain}"),
    ("post", "/api/v1/domains/{domain}/verify"),
    ("post", "/api/v1/domains/{domain}/disable"),
    ("post", "/api/v1/domains/{domain}/primary"),
]
BRANDING_MATRIX_PATHS = {path for _, path in BRANDING_MATRIX}


@pytest.mark.parametrize(("method", "path"), BRANDING_MATRIX)
def test_school_a_admin_cannot_touch_school_b_domains(world, other_world, as_member, method, path):
    import uuid

    foreign = _add(as_member(other_world.admin))
    client = as_member(world.admin)
    theirs = getattr(client, method)(path.format(domain=foreign["id"]))
    missing = getattr(client, method)(path.format(domain=uuid.uuid4()))
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["error"]["message"] == missing.json()["error"]["message"]
    assert SchoolDomain.objects.get(pk=foreign["id"]).status == "pending"
