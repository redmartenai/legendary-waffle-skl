from django.urls import path

from . import views

urlpatterns = [
    # Public (pre-authentication), rate-limited per IP.
    path("branding/resolve", views.BrandingResolveView.as_view(), name="branding-resolve"),
    path("branding/assets/<uuid:asset_id>", views.BrandingAssetView.as_view(), name="branding-asset"),
    # The X-School-Id school.
    path("branding", views.BrandingView.as_view(), name="branding"),
    path("branding/logo", views.BrandingLogoView.as_view(), name="branding-logo"),
    path("branding/favicon", views.BrandingFaviconView.as_view(), name="branding-favicon"),
    path("domains", views.DomainListView.as_view(), name="domain-list"),
    path("domains/<uuid:pk>", views.DomainDetailView.as_view(), name="domain-detail"),
    path("domains/<uuid:pk>/verify", views.DomainVerifyView.as_view(), name="domain-verify"),
    path("domains/<uuid:pk>/disable", views.DomainDisableView.as_view(), name="domain-disable"),
    path("domains/<uuid:pk>/primary", views.DomainPrimaryView.as_view(), name="domain-primary"),
    # Platform administration.
    path(
        "platform/schools/<uuid:school_id>/branding",
        views.PlatformBrandingView.as_view(),
        name="platform-branding",
    ),
    path(
        "platform/schools/<uuid:school_id>/branding/logo",
        views.PlatformLogoView.as_view(),
        name="platform-branding-logo",
    ),
    path(
        "platform/schools/<uuid:school_id>/branding/favicon",
        views.PlatformFaviconView.as_view(),
        name="platform-branding-favicon",
    ),
    path(
        "platform/schools/<uuid:school_id>/domains",
        views.PlatformDomainListView.as_view(),
        name="platform-domain-list",
    ),
    path(
        "platform/domains/<uuid:domain_id>",
        views.PlatformDomainDetailView.as_view(),
        name="platform-domain-detail",
    ),
    path(
        "platform/domains/<uuid:domain_id>/verify",
        views.PlatformDomainVerifyView.as_view(),
        name="platform-domain-verify",
    ),
    path(
        "platform/domains/<uuid:domain_id>/suspend",
        views.PlatformDomainSuspendView.as_view(),
        name="platform-domain-suspend",
    ),
]
