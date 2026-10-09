"""Edge cases of the image checks, the DNS verifiers, and service and view paths not reached elsewhere."""

import io
import json
import struct

import pytest
from django.core.files.storage import storages
from rest_framework.exceptions import ValidationError

from eduflow.branding import images, selectors, services, verification
from eduflow.branding.models import BrandAsset, SchoolDomain

from .conftest import ico, jpeg, png, upload

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------------------------------------ images
def _webp(chunk: bytes, body: bytes) -> bytes:
    payload = chunk + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WEBP" + payload


def test_lossy_and_lossless_webp_dimensions_are_read():
    lossy = _webp(b"VP8 ", b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", 120, 60) + b"\x00" * 4)
    assert (images.inspect(lossy, "logo").width, images.inspect(lossy, "logo").height) == (120, 60)
    bits = (200 - 1) | ((100 - 1) << 14)
    lossless = _webp(b"VP8L", b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 6)
    assert (images.inspect(lossless, "logo").width, images.inspect(lossless, "logo").height) == (200, 100)
    with pytest.raises(ValidationError):
        images.inspect(_webp(b"ABCD", b"\x00" * 20), "logo")
    with pytest.raises(ValidationError):
        images.inspect(b"RIFF\x00\x00\x00\x00WEBP", "logo")


def test_jpeg_parsing_skips_markers_and_refuses_damage():
    with_fill = b"\xff\xd8" + b"\xff\xff" + b"\xff\xd0" + jpeg()[2:]
    assert images.inspect(with_fill, "logo").width == 80
    for broken in (
        b"\xff\xd8\xff\xe0\x00\x01" + b"\x00" * 20,  # a segment shorter than its own length field
        b"\xff\xd8\x00\x00" + b"\x00" * 20,  # not a marker
        b"\xff\xd8\xff\xd9" + b"\x00" * 20,  # no frame
    ):
        with pytest.raises(ValidationError):
            images.inspect(broken, "logo")


def test_icons_are_checked():
    assert images.inspect(ico(48), "favicon").width == 48
    with pytest.raises(ValidationError):
        images.inspect(b"\x00\x00\x01\x00\x00\x00" + b"\x00" * 20, "favicon")  # no images inside
    assert images.inspect(png(32, 32), "favicon").content_type == "image/png"


# ------------------------------------------------------------------------------------------- DNS verifiers
class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_dns_over_https_reads_txt_answers(settings, monkeypatch):
    settings.DOMAIN_VERIFICATION_DOH_URL = "https://resolver.example/dns-query"
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"], seen["accept"], seen["timeout"] = request.full_url, request.headers["Accept"], timeout
        answer = {
            "Answer": [{"type": 16, "data": '"eduflow-domain-" "verification=abc"'}, {"type": 5, "data": "x"}]
        }
        return _Response(json.dumps(answer).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    records = verification.DnsOverHttpsVerifier().txt_records("_eduflow-challenge.portal.example")
    assert records == ["eduflow-domain-verification=abc"]
    assert seen["url"].startswith("https://resolver.example/dns-query?name=_eduflow-challenge.portal.example")
    assert (seen["accept"], seen["timeout"]) == ("application/dns-json", 5)


def test_dns_over_https_failures_are_inconclusive(settings, monkeypatch):
    verifier = verification.DnsOverHttpsVerifier()
    settings.DOMAIN_VERIFICATION_DOH_URL = "http://resolver.example/dns-query"  # never over plain http
    with pytest.raises(verification.VerificationUnavailable):
        verifier.txt_records("x.example")
    settings.DOMAIN_VERIFICATION_DOH_URL = "https://resolver.example/dns-query"

    def unreachable(request, timeout):
        raise OSError("down")

    monkeypatch.setattr("urllib.request.urlopen", unreachable)
    with pytest.raises(verification.VerificationUnavailable):
        verifier.txt_records("x.example")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: _Response(b"not json"))
    with pytest.raises(verification.VerificationUnavailable):
        verifier.txt_records("x.example")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: _Response(b"[]"))
    assert verifier.txt_records("x.example") == []


def test_the_disabled_verifier_never_answers():
    with pytest.raises(verification.VerificationUnavailable):
        verification.DisabledVerifier().txt_records("x.example")


def test_an_unreachable_resolver_is_a_503_and_no_conclusion(world, as_member, monkeypatch):
    admin = as_member(world.admin)
    created = admin.post("/api/v1/domains", {"hostname": "portal.example.org"}, format="json").json()

    def down(self, name):
        raise verification.VerificationUnavailable()

    monkeypatch.setattr(verification.MemoryVerifier, "txt_records", down)
    assert admin.post(f"/api/v1/domains/{created['id']}/verify").status_code == 503
    domain = SchoolDomain.objects.get()
    domain.status, domain.verified_at = "verified", domain.created_at
    domain.save()
    assert services.recheck_verified_domains() == {"checked": 0, "lapsed": 0}
    assert SchoolDomain.objects.get().check_failures == 0


def test_verifying_a_verified_domain_changes_nothing(world, as_member, dns):
    admin = as_member(world.admin)
    created = admin.post("/api/v1/domains", {"hostname": "portal.example.org"}, format="json").json()
    dns("portal.example.org", SchoolDomain.objects.get().verification_token)
    first = admin.post(f"/api/v1/domains/{created['id']}/verify").json()
    again = admin.post(f"/api/v1/domains/{created['id']}/verify").json()
    assert first["verified_at"] == again["verified_at"]


# -------------------------------------------------------------------------------------- services and views
def test_a_failed_save_removes_the_uploaded_object(world, monkeypatch):
    caller = services.Caller(user_id=world.admin.user.pk, membership=world.admin)

    def broken_create(**kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(BrandAsset.objects, "create", broken_create)
    with pytest.raises(RuntimeError):
        services.replace_asset(world.school, caller, "logo", png())
    assert storages["branding"].listdir(f"branding/{world.school.pk}")[1] == []


def test_a_failed_object_delete_is_logged_not_raised(
    world, as_member, monkeypatch, django_capture_on_commit_callbacks
):
    admin = as_member(world.admin)
    admin.put("/api/v1/branding/logo", {"file": upload(png())}, format="multipart")

    def refuse(key):
        raise OSError("storage down")

    monkeypatch.setattr(storages["branding"], "delete", refuse)
    with django_capture_on_commit_callbacks(execute=True):
        assert admin.delete("/api/v1/branding/logo").status_code == 200


def test_oversized_uploads_are_refused_before_reading(world, as_member):
    big = upload(b"\x00" * (256 * 1024 + 1))
    assert (
        as_member(world.admin).put("/api/v1/branding/favicon", {"file": big}, format="multipart").status_code
        == 400
    )


def test_school_wide_scope_is_required(world, as_member, make_member):
    from eduflow.authz.models import MembershipRole, Role
    from eduflow.authz.services import _set_grants, bump_rbac_version

    clerk = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key="brander", name="Brander")
    _set_grants(role, {"branding.manage": ["self"], "domain.manage": ["self"]})
    MembershipRole.objects.create(school=world.school, membership=clerk, role=role)
    bump_rbac_version(world.school.pk)
    client = as_member(clerk)
    assert client.patch("/api/v1/branding", {"primary_color": "#000000"}, format="json").status_code == 403
    assert client.get("/api/v1/domains").status_code == 403


def test_platform_assets_and_unknown_targets(world, platform_admin):
    import uuid

    path = f"/api/v1/platform/schools/{world.school.pk}/branding"
    added = platform_admin.put(f"{path}/logo", {"file": upload(png())}, format="multipart")
    assert added.status_code == 200
    assert added.json()["logo_url"]
    assert platform_admin.delete(f"{path}/logo").json()["logo_url"] is None
    assert platform_admin.get(f"/api/v1/platform/schools/{uuid.uuid4()}/branding").status_code == 404
    assert platform_admin.delete(f"/api/v1/platform/domains/{uuid.uuid4()}").status_code == 404


def test_malformed_asset_ids_and_resolution_of_an_inactive_schools_host(world, api_client):
    assert selectors.servable_asset("not-a-uuid") is None
    from eduflow.tenancy.models import School

    School.objects.filter(pk=world.school.pk).update(is_active=False)
    assert (
        api_client.get(f"/api/v1/branding/resolve?host={world.school.code}.eduflow.test").status_code == 404
    )
