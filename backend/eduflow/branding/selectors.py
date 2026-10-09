"""Branding reads. Everything here returns **public** fields only: colours, asset URLs, the school's name and
code. Never settings, contacts, storage keys or anything else from the school record.

Public (pre-authentication) reads run under the named, logged RLS bypass ``branding_public_read``, because
the caller has no school context yet. Authenticated reads run in the caller's school context.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db.models import Q

from eduflow.core import db_context
from eduflow.tenancy.models import School

from .models import BrandAsset, SchoolBranding

ASSET_PATH = "/api/v1/branding/assets/{id}"


def defaults() -> dict[str, str]:
    configured: dict[str, str] = dict(getattr(settings, "BRANDING_DEFAULTS", {}))
    return {
        "name": configured.get("name", "EduFlow"),
        "primary_color": configured.get("primary_color", "#1E40AF"),
        "secondary_color": configured.get("secondary_color", "#0EA5E9"),
    }


def _luminance(hex_color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def on_color(hex_color: str) -> str:
    """Black or white, whichever contrasts more with ``hex_color`` (WCAG relative luminance)."""
    lum = _luminance(hex_color)
    return "#000000" if (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) else "#FFFFFF"


def _asset_url(asset_id: Any) -> str | None:
    return ASSET_PATH.format(id=asset_id) if asset_id else None


def payload(school: School | None, branding: SchoolBranding | None) -> dict[str, Any]:
    base = defaults()
    primary = (branding.primary_color if branding else "") or base["primary_color"]
    secondary = (branding.secondary_color if branding else "") or base["secondary_color"]
    return {
        "school": (
            {"id": school.pk, "code": school.code, "name": school.name, "short_name": school.short_name}
            if school
            else None
        ),
        "display_name": (school.short_name or school.name) if school else base["name"],
        "primary_color": primary.upper(),
        "secondary_color": secondary.upper(),
        "on_primary": on_color(primary),
        "on_secondary": on_color(secondary),
        "logo_url": _asset_url(branding.logo_id if branding else None),
        "favicon_url": _asset_url(branding.favicon_id if branding else None),
        "version": branding.version if branding else 0,
        "is_default": branding is None
        or not (
            branding.primary_color or branding.secondary_color or branding.logo_id or branding.favicon_id
        ),
    }


def branding_of(school: School) -> SchoolBranding | None:
    """In the caller's school context (RLS applies)."""
    return SchoolBranding.objects.filter(school=school).first()


def public_branding(school: School | None) -> dict[str, Any]:
    if school is None:
        return payload(None, None)
    with db_context.system_context("branding_public_read"):
        branding = SchoolBranding.objects.filter(school=school).first()
    return payload(school, branding)


def school_by_id(school_id: uuid.UUID | None) -> School | None:
    return School.objects.filter(pk=school_id, is_active=True).first() if school_id else None


def servable_asset(asset_id: Any) -> BrandAsset | None:
    """An asset that is the *current* logo or favicon of an active school; anything else is not served."""
    try:
        asset_uuid = uuid.UUID(str(asset_id))
    except ValueError:
        return None
    with db_context.system_context("branding_public_read"):
        in_use = SchoolBranding.objects.filter(Q(logo_id=asset_uuid) | Q(favicon_id=asset_uuid)).filter(
            school__is_active=True
        )
        if not in_use.exists():
            return None
        return BrandAsset.objects.filter(pk=asset_uuid).first()
