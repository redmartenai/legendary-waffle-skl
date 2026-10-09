"""Root URL configuration.

Every API route is under ``/api/v1/`` with no trailing slash (docs/api/conventions.md).
Domain modules add their own ``api/urls.py``, included here as they are built.
"""

from django.conf import settings
from django.urls import URLPattern, URLResolver, include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from eduflow.core.health import LiveView, ReadyView
from eduflow.core.views import not_found

api_v1: list[URLPattern | URLResolver] = [
    path("health/live", LiveView.as_view(), name="health-live"),
    path("health/ready", ReadyView.as_view(), name="health-ready"),
    path("", include("eduflow.identity.api.urls")),
    path("", include("eduflow.tenancy.api.urls")),
    path("", include("eduflow.tenancy.api.platform_urls")),
    path("", include("eduflow.authz.api.urls")),
    path("", include("eduflow.audit.api.urls")),
    path("", include("eduflow.academics.api.urls")),
    path("", include("eduflow.people.api.urls")),
    path("", include("eduflow.invitations.api.urls")),
]

if settings.API_DOCS_ENABLED:
    api_v1 += [
        path("schema", SpectacularAPIView.as_view(), name="schema"),
        path("docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    ]

urlpatterns: list[URLPattern | URLResolver] = [
    path("api/v1/", include(api_v1)),
    # API-only server: any unmatched path gets the JSON error envelope, in every environment (in
    # DEBUG, Django would otherwise serve an HTML debug page that the client cannot parse).
    re_path(r"", not_found, name="not-found"),
]

handler400 = "eduflow.core.views.bad_request"
handler403 = "eduflow.core.views.permission_denied"
handler404 = "eduflow.core.views.not_found"
handler500 = "eduflow.core.views.server_error"
