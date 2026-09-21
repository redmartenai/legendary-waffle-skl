from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView


def healthz(_request):
    return JsonResponse({"status": "ok"})


admin.site.site_header = "EduFlow platform admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.api_urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("healthz", healthz),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
