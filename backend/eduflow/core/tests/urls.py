"""Test-only views that raise each kind of error, to check the error envelope end to end."""

from django.http import Http404
from django.urls import path
from rest_framework import exceptions, serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from config.urls import urlpatterns as project_urls


class _Body(serializers.Serializer):
    name = serializers.CharField(max_length=5)


class Boom(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, kind):
        if kind == "validation":
            _Body(data={"name": "far too long"}).is_valid(raise_exception=True)
        if kind == "validation-list":
            raise exceptions.ValidationError(["bad thing"])
        if kind == "throttled":
            raise exceptions.Throttled(wait=12.2)
        if kind == "denied":
            raise exceptions.PermissionDenied("secret internal reason")
        if kind == "django404":
            raise Http404("internal lookup detail")
        if kind == "crash":
            raise RuntimeError("db password=hunter2 exploded")
        return Response({"ok": True})


class Private(APIView):
    """Uses the project defaults: nothing authenticates in Phase 1, so this must be refused."""

    def get(self, request):
        return Response({"leaked": True})


# Test routes first: the project's catch-all 404 route must stay last.
urlpatterns = [
    path("test/boom/<str:kind>", Boom.as_view()),
    path("test/private", Private.as_view()),
    *project_urls,
]
