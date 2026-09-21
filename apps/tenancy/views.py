from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.core.utils import initials

from .models import School


def school_public(school: School) -> dict:
    return {
        "id": str(school.id),
        "code": school.code,
        "name": school.name,
        "short_name": school.short_name or school.name,
        "initials": initials(school.short_name or school.name),
        "kind": school.kind,
        "city": school.city,
        "branding": school.branding,
        "languages": school.languages or ["en"],
    }


class SchoolLookupView(APIView):
    """Find a school by the code printed on circulars (step 1 of sign-in)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def get(self, request):
        code = (request.query_params.get("code") or "").strip().upper()
        school = School.objects.filter(code=code, is_active=True).first() if code else None
        if school is None:
            return Response(
                {"error": {"code": "not_found", "message": "We couldn't find a school with that code."}},
                status=404,
            )
        return Response(school_public(school))
