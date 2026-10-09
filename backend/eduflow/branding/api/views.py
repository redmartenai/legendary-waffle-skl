"""Branding endpoints (ADR-029).

==============================  ============================================================================
Who                             Endpoints
==============================  ============================================================================
anyone (rate-limited per IP)    ``GET /branding/resolve?host=``, ``GET /branding/assets/{id}``
a member (``school.read``)      ``GET /branding`` (the ``X-School-Id`` school)
``branding.manage`` (school)    ``PATCH /branding``, ``PUT|DELETE /branding/logo|favicon``
``domain.manage`` (school)      ``/domains`` (list, add, read, remove, verify, disable, primary)
platform administrators         ``/platform/schools/{id}/branding[/logo|/favicon]``,
                                ``/platform/schools/{id}/domains``, ``/platform/domains/{id}/...``
==============================  ============================================================================

Assets are served by the API, never by storage URL: only an active school's *current* logo or favicon, with
its exact Content-Type, ``nosniff``, a sandboxing CSP and long-lived caching (asset IDs are immutable).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from eduflow.authz.api.base import PlatformAPIView, TenantAPIView
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors
from eduflow.identity.authentication import request_user
from eduflow.identity.throttles import IpThrottle
from eduflow.tenancy.models import School

from .. import hosts, images, selectors, services
from ..models import AssetKind, SchoolDomain
from . import serializers as s

TAG = ["branding"]
RULES: dict[str, str] = {
    AssetKind.LOGO: "PNG, JPEG or WebP, up to 1 MB, 16-4096 px",
    AssetKind.FAVICON: "square PNG or ICO, up to 256 KB, 16-512 px",
}
PLATFORM_TAG = ["platform"]
ASSET_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


class BrandingPublicIpThrottle(IpThrottle):
    scope = "branding_public_ip"


def _etag(data: dict[str, Any]) -> str:
    school = data["school"]["id"] if data["school"] else "platform"
    return f'"{school}:{data["version"]}"'


def _branding_response(request: Request, data: dict[str, Any], *, public: bool) -> Response:
    etag = _etag(data)
    response = Response(status=304) if request.headers.get("If-None-Match") == etag else Response(data)
    response["ETag"] = etag
    # Public answers depend only on the URL; authenticated ones on the school header and the caller.
    response["Cache-Control"] = "public, max-age=60" if public else "private, no-cache"
    response["Vary"] = "Origin" if public else "Authorization, X-School-Id"
    return response


# ------------------------------------------------------------------------------------------------ public
class BrandingResolveView(APIView):
    """Public: the branding of the school a hostname belongs to, for the sign-in screen."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [BrandingPublicIpThrottle]

    @extend_schema(
        tags=TAG,
        summary="Branding for a hostname (public)",
        description=(
            "Resolves the platform host, a school subdomain (`<code>.<base domain>`) or a **verified** "
            "custom domain. Unknown, pending, disabled and malformed hosts all answer `404`. Public fields "
            "only."
        ),
        parameters=[OpenApiParameter("host", OpenApiTypes.STR)],
        responses={200: s.ResolvedBrandingOut, 304: None, **errors(400, 404, 429)},
    )
    def get(self, request: Request) -> Response:
        query = s.ResolveQuery(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        match = hosts.classify(query.validated_data.get("host") or request.get_host(), allow_port=True)
        if match is None:
            raise NotFound()
        school = selectors.school_by_id(match.school_id)
        if match.kind != "platform" and school is None:
            raise NotFound()
        data = {**selectors.public_branding(school), "host_kind": match.kind}
        return _branding_response(request, data, public=True)


class BrandingAssetView(APIView):
    """Public: a current logo or favicon of an active school, by its immutable ID."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [BrandingPublicIpThrottle]

    @extend_schema(
        tags=TAG,
        summary="A brand image (public)",
        description="Only an active school's current logo or favicon is served; anything else is `404`.",
        responses={
            (200, "image/png"): OpenApiResponse(OpenApiTypes.BINARY, description="The image."),
            **errors(404, 429),
        },
    )
    def get(self, request: Request, asset_id: Any) -> HttpResponse:
        asset = selectors.servable_asset(asset_id)
        if asset is None:
            raise NotFound()
        response = HttpResponse(services.read_asset(asset), content_type=asset.content_type)
        response["Content-Disposition"] = f'inline; filename="{asset.kind}"'
        response["Content-Security-Policy"] = ASSET_CSP
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        response["Cross-Origin-Resource-Policy"] = "cross-origin"
        response["ETag"] = f'"{asset.sha256}"'
        return response


# ------------------------------------------------------------------------------------------------ school
def _require_school_wide(view: TenantAPIView, request: Request, permission: str) -> services.Caller:
    if DataScope.SCHOOL not in view.actor.scopes(permission):
        view.permission_denied(request)
    return services.Caller(user_id=view.actor.user.pk, membership=view.actor.membership)


def _upload(request: Request, kind: str) -> bytes:
    body = s.AssetUploadIn(data=request.data)
    body.is_valid(raise_exception=True)
    upload = body.validated_data["file"]
    if upload.size > images.LIMITS[AssetKind(kind)].max_bytes:
        raise ValidationError({"file": ["The file is too large."]})
    data: bytes = upload.read()
    return data


class BrandingView(TenantAPIView):
    required_permissions = {"GET": "school.read", "PATCH": "branding.manage"}

    @extend_schema(
        tags=TAG,
        summary="The current school's branding",
        description="Any member. Supports `If-None-Match` (the ETag changes with every branding change).",
        parameters=[TENANT_HEADER],
        responses={200: s.BrandingOut, 304: None, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        school = self.actor.school
        return _branding_response(
            request, selectors.payload(school, selectors.branding_of(school)), public=False
        )

    @extend_schema(
        tags=TAG,
        summary="Change the school's colours",
        description="Requires `branding.manage` school-wide. An empty value restores the platform default.",
        parameters=[TENANT_HEADER],
        request=s.ColorsIn,
        responses={200: s.BrandingOut, **errors(400, 401, 403)},
    )
    def patch(self, request: Request) -> Response:
        caller = _require_school_wide(self, request, "branding.manage")
        body = s.ColorsIn(data=request.data)
        body.is_valid(raise_exception=True)
        branding = services.update_colors(self.actor.school, caller, **body.validated_data)
        return Response(selectors.payload(self.actor.school, branding))


def _asset_view(kind: str) -> type[TenantAPIView]:
    class AssetView(TenantAPIView):
        required_permissions = {"PUT": "branding.manage", "DELETE": "branding.manage"}
        parser_classes = [MultiPartParser]

        @extend_schema(
            tags=TAG,
            summary=f"Upload the school's {kind}",
            description=(
                "Requires `branding.manage` school-wide. Multipart field `file`, checked by its content "
                f"({RULES[kind]}). SVG is refused."
            ),
            parameters=[TENANT_HEADER],
            request={"multipart/form-data": s.AssetUploadIn},
            responses={200: s.BrandingOut, **errors(400, 401, 403)},
        )
        def put(self, request: Request) -> Response:
            caller = _require_school_wide(self, request, "branding.manage")
            branding = services.replace_asset(self.actor.school, caller, kind, _upload(request, kind))
            return Response(selectors.payload(self.actor.school, branding))

        @extend_schema(
            tags=TAG,
            summary=f"Remove the school's {kind}",
            parameters=[TENANT_HEADER],
            responses={200: s.BrandingOut, **errors(401, 403)},
        )
        def delete(self, request: Request) -> Response:
            caller = _require_school_wide(self, request, "branding.manage")
            branding = services.remove_asset(self.actor.school, caller, kind)
            return Response(selectors.payload(self.actor.school, branding))

    AssetView.__name__ = AssetView.__qualname__ = f"Branding{kind.title()}View"
    return AssetView


BrandingLogoView = _asset_view(AssetKind.LOGO)
BrandingFaviconView = _asset_view(AssetKind.FAVICON)


class _SchoolDomainView(TenantAPIView):
    def load(self, request: Request, pk: Any) -> tuple[SchoolDomain, services.Caller]:
        caller = _require_school_wide(self, request, "domain.manage")
        domain = SchoolDomain.objects.for_school(self.actor.school).filter(pk=pk).first()
        if domain is None:
            raise NotFound()
        return domain, caller


class DomainListView(TenantAPIView):
    required_permissions = {"GET": "domain.manage", "POST": "domain.manage"}

    @extend_schema(
        tags=TAG,
        summary="The school's custom domains",
        parameters=[TENANT_HEADER],
        responses={200: s.DomainOut(many=True), **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        _require_school_wide(self, request, "domain.manage")
        domains = SchoolDomain.objects.for_school(self.actor.school).order_by("hostname")
        return Response(s.DomainOut(domains, many=True).data)

    @extend_schema(
        tags=TAG,
        summary="Register a custom domain",
        description=(
            "Requires `domain.manage` school-wide. The domain starts `pending`; publish the returned TXT "
            "record and call `/verify`. Platform domains cannot be registered; a domain already registered "
            "by any school is `409`."
        ),
        parameters=[TENANT_HEADER],
        request=s.DomainIn,
        responses={201: s.DomainOut, **errors(400, 401, 403, 409)},
    )
    def post(self, request: Request) -> Response:
        caller = _require_school_wide(self, request, "domain.manage")
        body = s.DomainIn(data=request.data)
        body.is_valid(raise_exception=True)
        domain = services.add_domain(self.actor.school, caller, body.validated_data["hostname"])
        return Response(s.DomainOut(domain).data, status=201)


class DomainDetailView(_SchoolDomainView):
    required_permissions = {"GET": "domain.manage", "DELETE": "domain.manage"}

    @extend_schema(
        tags=TAG,
        summary="A custom domain",
        parameters=[TENANT_HEADER],
        responses={200: s.DomainOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        return Response(s.DomainOut(self.load(request, pk)[0]).data)

    @extend_schema(
        tags=TAG,
        summary="Remove a custom domain",
        description="Not possible while the platform has suspended it (`409`).",
        parameters=[TENANT_HEADER],
        responses={204: None, **errors(401, 403, 404, 409)},
    )
    def delete(self, request: Request, pk: Any) -> Response:
        services.remove_domain(*self.load(request, pk))
        return Response(status=204)


def _domain_action(
    name: str, action: Callable[..., SchoolDomain], summary: str, description: str
) -> type[_SchoolDomainView]:
    class ActionView(_SchoolDomainView):
        required_permissions = {"POST": "domain.manage"}

        @extend_schema(
            tags=TAG,
            summary=summary,
            description=description,
            parameters=[TENANT_HEADER],
            request=None,
            responses={200: s.DomainOut, **errors(400, 401, 403, 404, 409, 503)},
        )
        def post(self, request: Request, pk: Any) -> Response:
            domain, caller = self.load(request, pk)
            return Response(s.DomainOut(action(domain, caller)).data)

    ActionView.__name__ = ActionView.__qualname__ = name
    return ActionView


DomainVerifyView = _domain_action(
    "DomainVerifyView",
    services.verify_domain,
    "Verify a custom domain now",
    "Checks the DNS TXT record. `400` while it is not found; `503` if automatic checks are not configured.",
)
DomainDisableView = _domain_action(
    "DomainDisableView",
    services.disable_domain,
    "Stop serving a custom domain",
    "The domain stops resolving at once. Verify it again to re-enable it.",
)
DomainPrimaryView = _domain_action(
    "DomainPrimaryView",
    services.set_primary,
    "Make a verified domain the primary one",
    "The primary domain is the one links and messages should use.",
)


# ------------------------------------------------------------------------------------------------ platform
def _platform_caller(request: Request) -> services.Caller:
    return services.Caller(user_id=request_user(request).pk, platform=True)


def _school(school_id: Any) -> School:
    school = School.objects.filter(pk=school_id).first()
    if school is None:
        raise NotFound()
    return school


def _platform_domain(domain_id: Any) -> SchoolDomain:
    domain = SchoolDomain.objects.select_related("school").filter(pk=domain_id).first()
    if domain is None:
        raise NotFound()
    return domain


class PlatformBrandingView(PlatformAPIView):
    @extend_schema(
        tags=PLATFORM_TAG,
        summary="A school's branding",
        responses={200: s.BrandingOut, **errors(401, 403, 404)},
    )
    def get(self, request: Request, school_id: Any) -> Response:
        school = _school(school_id)
        return Response(selectors.payload(school, selectors.branding_of(school)))

    @extend_schema(
        tags=PLATFORM_TAG,
        summary="Change a school's colours",
        request=s.ColorsIn,
        responses={200: s.BrandingOut, **errors(400, 401, 403, 404)},
    )
    def patch(self, request: Request, school_id: Any) -> Response:
        school = _school(school_id)
        body = s.ColorsIn(data=request.data)
        body.is_valid(raise_exception=True)
        branding = services.update_colors(school, _platform_caller(request), **body.validated_data)
        return Response(selectors.payload(school, branding))


def _platform_asset_view(kind: str) -> type[PlatformAPIView]:
    class PlatformAssetView(PlatformAPIView):
        parser_classes = [MultiPartParser]

        @extend_schema(
            tags=PLATFORM_TAG,
            summary=f"Upload a school's {kind}",
            request={"multipart/form-data": s.AssetUploadIn},
            responses={200: s.BrandingOut, **errors(400, 401, 403, 404)},
        )
        def put(self, request: Request, school_id: Any) -> Response:
            school = _school(school_id)
            branding = services.replace_asset(school, _platform_caller(request), kind, _upload(request, kind))
            return Response(selectors.payload(school, branding))

        @extend_schema(
            tags=PLATFORM_TAG,
            summary=f"Remove a school's {kind}",
            responses={200: s.BrandingOut, **errors(401, 403, 404)},
        )
        def delete(self, request: Request, school_id: Any) -> Response:
            school = _school(school_id)
            return Response(
                selectors.payload(school, services.remove_asset(school, _platform_caller(request), kind))
            )

    PlatformAssetView.__name__ = PlatformAssetView.__qualname__ = f"Platform{kind.title()}View"
    return PlatformAssetView


PlatformLogoView = _platform_asset_view(AssetKind.LOGO)
PlatformFaviconView = _platform_asset_view(AssetKind.FAVICON)


class PlatformDomainListView(PlatformAPIView):
    @extend_schema(
        tags=PLATFORM_TAG,
        summary="A school's custom domains",
        responses={200: s.DomainOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, school_id: Any) -> Response:
        school = _school(school_id)
        return Response(
            s.DomainOut(SchoolDomain.objects.filter(school=school).order_by("hostname"), many=True).data
        )

    @extend_schema(
        tags=PLATFORM_TAG,
        summary="Register a custom domain for a school",
        request=s.DomainIn,
        responses={201: s.DomainOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, school_id: Any) -> Response:
        body = s.DomainIn(data=request.data)
        body.is_valid(raise_exception=True)
        domain = services.add_domain(
            _school(school_id), _platform_caller(request), body.validated_data["hostname"]
        )
        return Response(s.DomainOut(domain).data, status=201)


class PlatformDomainDetailView(PlatformAPIView):
    @extend_schema(
        tags=PLATFORM_TAG,
        summary="Remove a custom domain",
        responses={204: None, **errors(401, 403, 404)},
    )
    def delete(self, request: Request, domain_id: Any) -> Response:
        services.remove_domain(_platform_domain(domain_id), _platform_caller(request))
        return Response(status=204)


class PlatformDomainVerifyView(PlatformAPIView):
    @extend_schema(
        tags=PLATFORM_TAG,
        summary="Verify a custom domain by hand",
        description=(
            "For use after confirming ownership out of band (for example while no automatic DNS verifier is "
            "configured). The note is audited. Also lifts a platform suspension."
        ),
        request=s.PlatformVerifyIn,
        responses={200: s.DomainOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, domain_id: Any) -> Response:
        body = s.PlatformVerifyIn(data=request.data)
        body.is_valid(raise_exception=True)
        domain = services.platform_verify(
            _platform_domain(domain_id), _platform_caller(request), body.validated_data["note"]
        )
        return Response(s.DomainOut(domain).data)


class PlatformDomainSuspendView(PlatformAPIView):
    @extend_schema(
        tags=PLATFORM_TAG,
        summary="Suspend a custom domain",
        description=(
            "The domain stops resolving; the school cannot re-verify or remove it until the platform does."
        ),
        request=s.DisableIn,
        responses={200: s.DomainOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, domain_id: Any) -> Response:
        body = s.DisableIn(data=request.data)
        body.is_valid(raise_exception=True)
        domain = services.disable_domain(
            _platform_domain(domain_id), _platform_caller(request), body.validated_data.get("reason", "")
        )
        return Response(s.DomainOut(domain).data)
