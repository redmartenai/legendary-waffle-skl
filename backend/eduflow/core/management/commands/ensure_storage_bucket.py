"""Create the private object-storage bucket if it does not exist. Safe to rerun.

Used by the local Docker stack. In production, buckets are provisioned by infrastructure tooling,
and the application credentials should not be allowed to create them.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from eduflow.core.health import storage_client


class Command(BaseCommand):
    help = "Create the configured private storage bucket if missing (idempotent)."

    def handle(self, *args: Any, **options: Any) -> None:
        bucket = settings.STORAGE_BUCKET
        client = storage_client()
        try:
            client.head_bucket(Bucket=bucket)
            self.stdout.write(f"bucket '{bucket}' already exists")
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in ("404", "NoSuchBucket", "NotFound"):
                raise CommandError(f"cannot access bucket '{bucket}': {code}") from exc

        # No ACL and no bucket policy: new buckets are private. Access is only ever through
        # API-authorized, short-lived signed URLs (ADR-009).
        client.create_bucket(Bucket=bucket)
        self.stdout.write(f"bucket '{bucket}' created (private)")
