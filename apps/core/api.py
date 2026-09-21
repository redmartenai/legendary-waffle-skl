"""Base API view for school-scoped endpoints."""

import uuid

from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.views import APIView

from .tenant import activate_school, deactivate_school, unscoped

SCHOOL_HEADER = "HTTP_X_SCHOOL_ID"


def resolve_school(request):
    """Pick the active school from the X-School-Id header and the user's memberships."""
    from apps.accounts.models import Membership

    raw = request.META.get(SCHOOL_HEADER) or request.query_params.get("school")
    with unscoped():
        memberships = list(
            Membership.all_objects.select_related("school").filter(
                user=request.user, is_active=True, school__is_active=True
            )
        )
    if raw:
        try:
            school_id = uuid.UUID(str(raw))
        except ValueError as exc:
            raise ValidationError({"school": "Invalid school id."}) from exc
        memberships = [m for m in memberships if m.school_id == school_id]
        if not memberships:
            raise PermissionDenied("You are not a member of this school.")
    else:
        school_ids = {m.school_id for m in memberships}
        if not school_ids:
            raise PermissionDenied("Your account has no active school.")
        if len(school_ids) > 1:
            raise ValidationError({"school": "Choose a school with the X-School-Id header."})
    return memberships[0].school, memberships


class SchoolAPIView(APIView):
    """Authenticates, activates the tenant context and checks roles.

    Subclasses set ``allowed_roles`` to restrict who may call them. The school
    context is always cleared when the response is finalized.
    """

    allowed_roles: frozenset[str] | None = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        school, memberships = resolve_school(request)
        request.school = school
        request.memberships = memberships
        request.roles = frozenset(m.role for m in memberships)
        self._school_token = activate_school(school)
        if self.allowed_roles is not None and not (request.roles & self.allowed_roles):
            raise PermissionDenied("This isn't available for your role.")

    def finalize_response(self, request, response, *args, **kwargs):
        token = getattr(self, "_school_token", None)
        if token is not None:
            deactivate_school(token)
            self._school_token = None
        return super().finalize_response(request, response, *args, **kwargs)


def get_scoped_or_404(model, **lookup):
    """get_object_or_404 through the tenant-scoped manager; invalid UUIDs become 404."""
    try:
        return get_object_or_404(model.objects, **lookup)
    except (ValueError, ValidationError) as exc:
        from django.http import Http404

        raise Http404 from exc


def parse_uuid(value, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError({field: "Invalid id."}) from exc
