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
        "campus": (school.settings or {}).get("campus"),
        "address": (school.settings or {}).get("address"),
        "contacts": (school.settings or {}).get("contacts") or {},
        "academic_year": _current_year_name(school),
        "term": _current_term(school),
        "languages": school.languages or ["en"],
    }


def _current_term(school: School) -> str | None:
    """The term running today, from `settings.terms` ([{name, starts_on, ends_on}])."""
    from apps.core.utils import school_today

    today = school_today(school).isoformat()
    for term in (school.settings or {}).get("terms") or []:
        if term.get("starts_on", "") <= today <= term.get("ends_on", ""):
            return term.get("name")
    return None


def _current_year_name(school: School) -> str | None:
    from apps.academics.models import AcademicYear
    from apps.core.tenant import unscoped

    with unscoped():
        year = AcademicYear.all_objects.filter(school=school, is_current=True).values_list("name", flat=True).first()
    return year


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
