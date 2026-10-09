"""Invitation reads. Always through the scoped resource, with what the API renders loaded up front."""

from __future__ import annotations

from django.db.models import QuerySet

from .models import Invitation


def invitation_queryset() -> QuerySet[Invitation]:
    return Invitation.objects.select_related(
        "invited_by__user", "student", "guardian", "department", "campus"
    ).prefetch_related("roles")
