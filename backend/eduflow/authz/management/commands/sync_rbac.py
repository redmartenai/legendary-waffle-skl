"""Sync the permission catalogue and add new default grants to every school's system roles.

Run after ``migrate`` on each deployment. It never removes a grant or changes a grant's scopes. It does add
every default grant a system role is missing, so a default that a school removed from a system role comes
back;
schools that need a narrower role should use a custom role (``based_on`` the system role) instead.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from eduflow.authz.services import seed_system_roles, sync_permission_catalog
from eduflow.tenancy.models import School


class Command(BaseCommand):
    help = "Sync permissions and system roles for all schools (additive only)."

    def handle(self, *args: Any, **options: Any) -> None:
        sync_permission_catalog()
        count = 0
        for school in School.objects.order_by("code"):
            with transaction.atomic():
                seed_system_roles(school, only_missing_grants=True)
            count += 1
        self.stdout.write(
            self.style.SUCCESS(f"Permission catalogue synced; system roles checked in {count} school(s).")
        )
