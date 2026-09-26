"""Create the Postgres schema Django's tables live in (``DATABASE_SCHEMA``), so ``migrate`` can run.

On Supabase this keeps the tables out of ``public``, which Supabase exposes through its REST API.
"""

import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Create the DATABASE_SCHEMA schema on Postgres if it doesn't exist (no-op on SQLite)."

    def handle(self, *args, **options):
        schema = getattr(settings, "DATABASE_SCHEMA", "")
        if connection.vendor != "postgresql" or not schema:
            self.stdout.write("Nothing to do: not Postgres, or DATABASE_SCHEMA is empty.")
            return
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise CommandError("DATABASE_SCHEMA must be a lowercase identifier, e.g. eduflow")
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        self.stdout.write(f"Schema {schema!r} is ready.")
