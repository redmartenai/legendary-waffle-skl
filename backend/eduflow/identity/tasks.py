"""Identity maintenance tasks. Identity tables are platform-owned (no RLS), so no tenant context is needed."""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from eduflow.core.logging import get_logger

from .models import AuthSession, OtpChallenge

log = get_logger(__name__)


@shared_task(name="eduflow.identity.tasks.purge_expired_auth_records", ignore_result=True)
def purge_expired_auth_records() -> dict[str, int]:
    """Delete OTP challenges and sessions that ended more than ``AUTH_RECORD_RETENTION_DAYS`` ago.

    The audit trail keeps the history; these rows are only needed while they can still be used.
    """
    cutoff = timezone.now() - timedelta(days=int(settings.AUTH_RECORD_RETENTION_DAYS))
    otp_deleted, _ = OtpChallenge.objects.filter(expires_at__lt=cutoff).delete()
    sessions_deleted, _ = AuthSession.objects.filter(
        Q(expires_at__lt=cutoff) | Q(revoked_at__lt=cutoff)
    ).delete()
    log.info("auth_records_purged", otp_challenges=otp_deleted, sessions=sessions_deleted)
    return {"otp_challenges": otp_deleted, "sessions": sessions_deleted}
