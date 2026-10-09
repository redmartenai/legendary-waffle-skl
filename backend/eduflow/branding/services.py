"""Branding and domain writes (ADR-029). Transactional, validated and audited (``branding.*``).

Callers are either a school member acting in their own school (``by`` = their membership) or a platform
administrator (``platform=True``, ``by=None``). Both pass the target ``school`` explicitly; the views decide
who may call (``branding.manage`` / ``domain.manage`` school-wide, or ``is_platform_admin``).

Asset objects live in the ``branding`` storage under ``branding/<school_id>/<asset_id>.<ext>``: no part of the
key comes from the client. A replaced asset's object is deleted after the transaction commits; its database
row stays as history.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from eduflow.audit import services as audit
from eduflow.core.api import Conflict, ServiceUnavailable
from eduflow.core.ids import uuid7
from eduflow.core.logging import get_logger
from eduflow.tenancy.models import Membership, School

from . import hosts, images, verification
from .models import BrandAsset, DomainStatus, SchoolBranding, SchoolDomain

log = get_logger(__name__)
RECHECK_FAILURE_LIMIT = 3


@dataclass(frozen=True)
class Caller:
    """Who is acting: a member of the school, or a platform administrator."""

    user_id: uuid.UUID
    membership: Membership | None = None
    platform: bool = False


def _record(action: str, school: School, caller: Caller, target: Any, **metadata: Any) -> None:
    audit.record(
        action,
        actor_id=caller.user_id,
        school_id=school.pk,
        target_type=target.__class__.__name__.lower(),
        target_id=target.pk,
        metadata={
            k: v for k, v in {**metadata, "by_platform": caller.platform or None}.items() if v is not None
        }
        or None,
    )


def storage() -> Any:
    return storages["branding"]


# --------------------------------------------------------------------------------------------- branding
def _locked_branding(school: School) -> SchoolBranding:
    SchoolBranding.objects.get_or_create(school=school)
    return SchoolBranding.objects.select_for_update(of=("self",)).get(school=school)


@transaction.atomic
def update_colors(school: School, caller: Caller, **data: str) -> SchoolBranding:
    branding = _locked_branding(school)
    changed = [field for field, value in data.items() if getattr(branding, field) != value.upper()]
    for field in changed:
        setattr(branding, field, data[field].upper())
    if changed:
        branding.version += 1
        branding.updated_by = caller.membership
        branding.save()
        _record("branding.updated", school, caller, branding, fields=changed)
    return branding


def _delete_object_later(key: str) -> None:
    def delete() -> None:
        try:
            storage().delete(key)
        except Exception:  # best effort: an orphaned private object is harmless and logged
            log.warning("branding_asset_delete_failed")

    transaction.on_commit(delete)


@transaction.atomic
def replace_asset(school: School, caller: Caller, kind: str, data: bytes) -> SchoolBranding:
    info = images.inspect(data, kind)
    branding = _locked_branding(school)
    asset_id = uuid7()
    key = f"branding/{school.pk}/{asset_id}.{info.extension}"
    # Saved before the row; if the transaction then fails, the object is an unreferenced private orphan.
    saved_key = storage().save(key, ContentFile(data))
    try:
        asset = BrandAsset.objects.create(
            id=asset_id,
            school=school,
            kind=kind,
            content_type=info.content_type,
            size=len(data),
            width=info.width,
            height=info.height,
            sha256=info.sha256,
            storage_key=saved_key,
            uploaded_by=caller.membership,
        )
    except Exception:
        storage().delete(saved_key)
        raise
    previous: BrandAsset | None = getattr(branding, kind)
    setattr(branding, kind, asset)
    branding.version += 1
    branding.updated_by = caller.membership
    branding.save()
    if previous is not None:
        _delete_object_later(previous.storage_key)
    _record(
        f"branding.{kind}.replaced",
        school,
        caller,
        branding,
        asset=str(asset.pk),
        content_type=info.content_type,
        size=len(data),
        width=info.width,
        height=info.height,
    )
    return branding


@transaction.atomic
def remove_asset(school: School, caller: Caller, kind: str) -> SchoolBranding:
    branding = _locked_branding(school)
    previous: BrandAsset | None = getattr(branding, kind)
    if previous is None:
        return branding
    setattr(branding, kind, None)
    branding.version += 1
    branding.updated_by = caller.membership
    branding.save()
    _delete_object_later(previous.storage_key)
    _record(f"branding.{kind}.removed", school, caller, branding, asset=str(previous.pk))
    return branding


def read_asset(asset: BrandAsset) -> bytes:
    with storage().open(asset.storage_key, "rb") as handle:
        data: bytes = handle.read()
    return data


# --------------------------------------------------------------------------------------------- domains
_TAKEN = "This domain is already registered."


def _check_registrable(hostname: str) -> str:
    try:
        host = hosts.normalize(hostname)
    except hosts.InvalidHostname:
        raise ValidationError({"hostname": ["Enter a domain name such as portal.example.edu."]}) from None
    if hosts.is_platform_namespace(host):
        raise ValidationError({"hostname": ["Domains of the EduFlow platform cannot be registered."]})
    return host


@transaction.atomic
def add_domain(school: School, caller: Caller, hostname: str) -> SchoolDomain:
    host = _check_registrable(hostname)
    try:
        with transaction.atomic():
            domain = SchoolDomain.objects.create(
                school=school,
                hostname=host,
                verification_token=secrets.token_urlsafe(24),
                created_by=caller.membership,
            )
    except IntegrityError:
        raise Conflict(_TAKEN) from None
    _record("branding.domain.added", school, caller, domain, hostname=host)
    return domain


def _locked_domain(domain: SchoolDomain) -> SchoolDomain:
    return SchoolDomain.objects.select_for_update(of=("self",)).select_related("school").get(pk=domain.pk)


def _ensure_not_suspended(domain: SchoolDomain, caller: Caller) -> None:
    if domain.suspended_by_platform and not caller.platform:
        raise Conflict("The platform has suspended this domain. Contact EduFlow support.")


def _mark_verified(domain: SchoolDomain) -> None:
    domain.status = DomainStatus.VERIFIED
    domain.verified_at = domain.last_checked_at = timezone.now()
    domain.check_failures = 0
    domain.disabled_reason = ""
    domain.suspended_by_platform = False
    domain.save()
    hosts.forget(domain.hostname)


def verify_domain(domain: SchoolDomain, caller: Caller) -> SchoolDomain:
    """Check the DNS TXT proof now. ``400`` if it is not there yet; ``503`` if no verifier is configured.

    A failed check is committed (``last_checked_at`` and its audit event) before the ``400`` is raised.
    """
    with transaction.atomic():
        domain = _locked_domain(domain)
        _ensure_not_suspended(domain, caller)
        if domain.status == DomainStatus.VERIFIED:
            return domain
        if not verification.verifier().enabled:
            raise ServiceUnavailable("Automatic domain verification is not configured. Ask EduFlow support.")
        try:
            found = verification.has_challenge(domain.hostname, domain.verification_token)
        except verification.VerificationUnavailable:
            raise ServiceUnavailable("The DNS check could not be completed. Try again later.") from None
        domain.last_checked_at = timezone.now()
        if found:
            _mark_verified(domain)
            _record(
                "branding.domain.verified",
                domain.school,
                caller,
                domain,
                hostname=domain.hostname,
                method="dns",
            )
            return domain
        domain.save(update_fields=["last_checked_at", "updated_at"])
        _record(
            "branding.domain.verification_failed", domain.school, caller, domain, hostname=domain.hostname
        )
    raise ValidationError(
        {"hostname": [f"The TXT record {verification.challenge_name(domain.hostname)} was not found yet."]}
    )


@transaction.atomic
def platform_verify(domain: SchoolDomain, caller: Caller, note: str) -> SchoolDomain:
    """Manual verification by EduFlow staff after checking ownership out of band (audited, with a note)."""
    domain = _locked_domain(domain)
    _mark_verified(domain)
    _record(
        "branding.domain.verified",
        domain.school,
        caller,
        domain,
        hostname=domain.hostname,
        method="manual",
        note=note,
    )
    return domain


@transaction.atomic
def disable_domain(domain: SchoolDomain, caller: Caller, reason: str = "") -> SchoolDomain:
    domain = _locked_domain(domain)
    _ensure_not_suspended(domain, caller)
    domain.status = DomainStatus.DISABLED
    domain.is_primary = False
    domain.disabled_reason = reason or ("platform" if caller.platform else "owner")
    domain.suspended_by_platform = caller.platform
    domain.save()
    hosts.forget(domain.hostname)
    _record(
        "branding.domain.disabled", domain.school, caller, domain, hostname=domain.hostname, reason=reason
    )
    return domain


@transaction.atomic
def set_primary(domain: SchoolDomain, caller: Caller) -> SchoolDomain:
    domain = _locked_domain(domain)
    if domain.status != DomainStatus.VERIFIED:
        raise Conflict("Only a verified domain can be the primary one.")
    SchoolDomain.objects.filter(school_id=domain.school_id, is_primary=True).exclude(pk=domain.pk).update(
        is_primary=False
    )
    domain.is_primary = True
    domain.save(update_fields=["is_primary", "updated_at"])
    _record("branding.domain.primary_set", domain.school, caller, domain, hostname=domain.hostname)
    return domain


@transaction.atomic
def remove_domain(domain: SchoolDomain, caller: Caller) -> None:
    domain = _locked_domain(domain)
    _ensure_not_suspended(domain, caller)  # else removing and re-adding would lift a suspension
    hostname, school, pk = domain.hostname, domain.school, domain.pk
    domain.delete()
    domain.pk = pk
    hosts.forget(hostname)
    _record("branding.domain.removed", school, caller, domain, hostname=hostname)


def recheck_verified_domains(limit: int = 500) -> dict[str, int]:
    """Daily: a verified domain whose TXT proof is gone for ``RECHECK_FAILURE_LIMIT`` checks is disabled.

    Runs under the logged bypass (it is a platform-wide job) and records each lapse in the audit trail.
    """
    from eduflow.core import db_context

    stats = {"checked": 0, "lapsed": 0}
    if not verification.verifier().enabled:
        return stats
    with db_context.system_context("branding_domain_recheck"):
        ids = list(
            SchoolDomain.objects.filter(status=DomainStatus.VERIFIED)
            .order_by("last_checked_at")[:limit]
            .values_list("pk", flat=True)
        )
        for pk in ids:
            with transaction.atomic():
                domain = (
                    SchoolDomain.objects.select_for_update(skip_locked=True, of=("self",))
                    .filter(pk=pk)
                    .first()
                )
                if domain is None or domain.status != DomainStatus.VERIFIED:
                    continue
                try:
                    present = verification.has_challenge(domain.hostname, domain.verification_token)
                except verification.VerificationUnavailable:
                    continue  # the resolver is down: no conclusion about the domain
                stats["checked"] += 1
                domain.last_checked_at = timezone.now()
                domain.check_failures = 0 if present else domain.check_failures + 1
                if domain.check_failures >= RECHECK_FAILURE_LIMIT:
                    domain.status = DomainStatus.DISABLED
                    domain.is_primary = False
                    domain.disabled_reason = "verification_lapsed"
                    stats["lapsed"] += 1
                    audit.record(
                        "branding.domain.lapsed",
                        actor_id=None,
                        school_id=domain.school_id,
                        target_type="schooldomain",
                        target_id=domain.pk,
                        metadata={"hostname": domain.hostname},
                    )
                domain.save()
                hosts.forget(domain.hostname)
    return stats
