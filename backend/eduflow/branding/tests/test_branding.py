"""School branding: defaults, colours, logo and favicon uploads, serving, and who may change what."""

import pytest
from django.core.files.storage import storages

from eduflow.audit.models import AuditEvent
from eduflow.branding.models import BrandAsset, SchoolBranding

from .conftest import ico, jpeg, png, upload, webp

pytestmark = pytest.mark.django_db


def _put(client, kind, data, **kw):
    return client.put(f"/api/v1/branding/{kind}", {"file": upload(data, **kw)}, format="multipart")


# ------------------------------------------------------------------------------------------------ defaults
def test_a_school_without_branding_gets_the_platform_defaults(world, as_member):
    data = as_member(world.teacher).get("/api/v1/branding").json()
    assert data["school"]["code"] == world.school.code
    assert data["display_name"] == world.school.name
    assert (data["primary_color"], data["secondary_color"]) == ("#1E40AF", "#0EA5E9")
    assert (data["logo_url"], data["favicon_url"], data["version"], data["is_default"]) == (
        None,
        None,
        0,
        True,
    )
    assert data["on_primary"] == "#FFFFFF"  # dark blue: white text
    assert not SchoolBranding.objects.exists()


def test_the_short_name_is_the_display_name(world, as_member):
    world.school.short_name = "GVS"
    world.school.save()
    assert as_member(world.parent).get("/api/v1/branding").json()["display_name"] == "GVS"


# ------------------------------------------------------------------------------------------------ colours
def test_the_school_admin_changes_colours(world, as_member):
    admin = as_member(world.admin)
    response = admin.patch(
        "/api/v1/branding", {"primary_color": "#ffd700", "secondary_color": "#004d40"}, format="json"
    )
    assert response.status_code == 200, response.content
    data = response.json()
    assert (data["primary_color"], data["on_primary"]) == ("#FFD700", "#000000")  # gold: black text
    assert (data["version"], data["is_default"]) == (2, False)
    event = AuditEvent.objects.get(action="branding.updated")
    assert sorted(event.metadata["fields"]) == ["primary_color", "secondary_color"]
    reset = admin.patch("/api/v1/branding", {"primary_color": ""}, format="json").json()
    assert reset["primary_color"] == "#1E40AF"
    same = admin.patch("/api/v1/branding", {"secondary_color": "#004D40"}, format="json").json()
    assert same["version"] == reset["version"]  # no change, no new version


@pytest.mark.parametrize("value", ["red", "#FFF", "#GGGGGG", "ffd700", "#ffd7000", "url(javascript:1)"])
def test_colours_must_be_hex(world, as_member, value):
    response = as_member(world.admin).patch("/api/v1/branding", {"primary_color": value}, format="json")
    assert response.status_code == 400
    assert "primary_color" in response.json()["error"]["fields"]


def test_unknown_fields_are_refused(world, as_member):
    response = as_member(world.admin).patch(
        "/api/v1/branding", {"school_id": "x", "logo_id": "y"}, format="json"
    )
    assert response.status_code == 400


@pytest.mark.parametrize("role", ["teacher", "parent", "student_member"])
def test_only_school_administrators_change_branding(world, as_member, role):
    client = as_member(getattr(world, role))
    assert client.patch("/api/v1/branding", {"primary_color": "#000000"}, format="json").status_code == 403
    assert _put(client, "logo", png()).status_code == 403
    assert client.delete("/api/v1/branding/logo").status_code == 403
    assert client.get("/api/v1/branding").status_code == 200  # every member reads it


def test_the_principal_changes_branding(world, as_member):
    assert (
        as_member(world.principal)
        .patch("/api/v1/branding", {"primary_color": "#123456"}, format="json")
        .status_code
        == 200
    )


def test_a_school_admin_only_brands_their_own_school(world, other_world, client_for):
    client = client_for(world.admin.user, other_world.school)  # forged X-School-Id
    response = client.patch("/api/v1/branding", {"primary_color": "#000000"}, format="json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"
    assert not SchoolBranding.objects.filter(school=other_world.school).exists()


# ------------------------------------------------------------------------------------------------ assets
@pytest.mark.parametrize(
    ("data", "content_type"), [(png(), "image/png"), (jpeg(), "image/jpeg"), (webp(), "image/webp")]
)
def test_logo_formats_are_identified_by_content(world, as_member, api_client, data, content_type):
    admin = as_member(world.admin)
    # The filename and declared type are ignored: the bytes decide.
    response = _put(admin, "logo", data, name="logo.gif", content_type="text/html")
    assert response.status_code == 200, response.content
    url = response.json()["logo_url"]
    served = api_client.get(url)
    assert served.status_code == 200
    assert served["Content-Type"] == content_type
    assert b"".join(served) == data
    assert "sandbox" in served["Content-Security-Policy"]
    assert served["X-Content-Type-Options"] == "nosniff"
    assert "immutable" in served["Cache-Control"]
    asset = BrandAsset.objects.get()
    assert asset.storage_key.startswith(f"branding/{world.school.pk}/")
    assert str(asset.storage_key) not in str(response.content)  # storage paths never leave the server


@pytest.mark.parametrize(
    ("data", "kind", "message"),
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "logo", "SVG"),
        (b"GIF89a" + b"\x00" * 20, "logo", "SVG"),
        (b"<html><script>alert(1)</script></html>", "logo", "SVG"),
        (b"", "logo", "empty"),
        (png(8, 8), "logo", "pixels"),
        (png(5000, 20), "logo", "pixels"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, "logo", "damaged"),
        (png(64, 32), "favicon", "square"),
        (jpeg(), "favicon", "favicon"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024 + 1), "logo", "large"),
    ],
)
def test_unsafe_or_malformed_images_are_refused(world, as_member, data, kind, message):
    response = _put(as_member(world.admin), kind, data)
    assert response.status_code == 400, response.content
    assert message.lower() in " ".join(response.json()["error"]["fields"]["file"]).lower()
    assert not BrandAsset.objects.exists()


def test_a_favicon_may_be_ico_or_png(world, as_member, api_client):
    admin = as_member(world.admin)
    icon = _put(admin, "favicon", ico()).json()
    assert api_client.get(icon["favicon_url"])["Content-Type"] == "image/x-icon"
    square = _put(admin, "favicon", png(48, 48)).json()
    assert api_client.get(square["favicon_url"]).status_code == 200


def test_replacing_a_logo_retires_the_old_one(
    world, as_member, api_client, django_capture_on_commit_callbacks
):
    admin = as_member(world.admin)
    first = _put(admin, "logo", png()).json()
    old = BrandAsset.objects.get()
    with django_capture_on_commit_callbacks(execute=True):
        second = _put(admin, "logo", jpeg()).json()
    assert second["logo_url"] != first["logo_url"]
    assert second["version"] == first["version"] + 1
    assert api_client.get(first["logo_url"]).status_code == 404  # no longer served
    assert not storages["branding"].exists(old.storage_key)  # and its object is gone
    assert BrandAsset.objects.count() == 2  # the row stays as history
    assert AuditEvent.objects.filter(action="branding.logo.replaced").count() == 2


def test_removing_a_logo(world, as_member, api_client, django_capture_on_commit_callbacks):
    admin = as_member(world.admin)
    url = _put(admin, "logo", png()).json()["logo_url"]
    with django_capture_on_commit_callbacks(execute=True):
        removed = admin.delete("/api/v1/branding/logo").json()
    assert removed["logo_url"] is None
    assert api_client.get(url).status_code == 404
    assert admin.delete("/api/v1/branding/logo").status_code == 200  # nothing to remove: unchanged


def test_assets_are_not_served_for_inactive_schools_or_unknown_ids(world, as_member, api_client):
    import uuid

    url = _put(as_member(world.admin), "logo", png()).json()["logo_url"]
    assert api_client.get(f"/api/v1/branding/assets/{uuid.uuid4()}").status_code == 404
    world.school.is_active = False
    world.school.save()
    assert api_client.get(url).status_code == 404


def test_one_schools_admin_cannot_point_at_another_schools_asset(world, other_world, as_member):
    _put(as_member(other_world.admin), "logo", png())
    foreign = BrandAsset.objects.get(school=other_world.school)
    from django.db import IntegrityError, transaction

    branding = SchoolBranding.objects.create(school=world.school)
    branding.logo = foreign
    with pytest.raises(IntegrityError, match="branding_school_branding_logo_school_fk"), transaction.atomic():
        branding.save()


# ------------------------------------------------------------------------------------------------ caching
def test_etags_change_with_the_branding(world, as_member):
    client = as_member(world.teacher)
    first = client.get("/api/v1/branding")
    etag = first["ETag"]
    assert first["Cache-Control"] == "private, no-cache"
    assert "X-School-Id" in first["Vary"]
    assert client.get("/api/v1/branding", HTTP_IF_NONE_MATCH=etag).status_code == 304
    as_member(world.admin).patch("/api/v1/branding", {"primary_color": "#101010"}, format="json")
    changed = client.get("/api/v1/branding", HTTP_IF_NONE_MATCH=etag)
    assert changed.status_code == 200
    assert changed["ETag"] != etag


def test_branding_never_leaks_between_schools(world, other_world, as_member):
    as_member(world.admin).patch("/api/v1/branding", {"primary_color": "#111111"}, format="json")
    as_member(other_world.admin).patch("/api/v1/branding", {"primary_color": "#222222"}, format="json")
    assert as_member(world.parent).get("/api/v1/branding").json()["primary_color"] == "#111111"
    assert as_member(other_world.parent).get("/api/v1/branding").json()["primary_color"] == "#222222"
