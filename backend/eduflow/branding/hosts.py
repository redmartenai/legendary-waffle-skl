"""Hostnames: normalisation, and which school (if any) a host belongs to (ADR-029).

==========  ==========================================================================================
Kind        Rule
==========  ==========================================================================================
platform    one of ``WHITE_LABEL_PLATFORM_HOSTS`` or the base domain itself: no school
subdomain   ``<code>.<WHITE_LABEL_BASE_DOMAIN>``: the active school with that (immutable) code
custom      a ``SchoolDomain`` row in status ``verified`` for an active school
==========  ==========================================================================================

Anything else (unknown, pending, disabled, reserved label, malformed) resolves to nothing. **A host is never
proof of authorisation**: it only selects which school a request is about; membership is still checked.

Lookups are cached per host for a short time (``branding:host:<sha256>``); every domain status change
deletes the entry for that host (``forget``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from eduflow.core import db_context
from eduflow.tenancy.models import School

from .models import DomainStatus, SchoolDomain

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
CACHE_SECONDS = 300
MISS_CACHE_SECONDS = 60
_MISS = "-"


class InvalidHostname(ValueError):
    pass


def normalize(raw: str, *, allow_port: bool = False) -> str:
    """Lower-case ASCII (IDNA) form of a hostname, or ``InvalidHostname``. No IPs, no wildcards."""
    host = (raw or "").strip().lower().rstrip(".")
    if allow_port and host.count(":") == 1:
        host = host.split(":", 1)[0]
    if not host or len(host) > 253 or "*" in host or ":" in host or "/" in host:
        raise InvalidHostname(raw)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InvalidHostname(raw)  # an IP address is not a domain
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidHostname(raw) from None
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise InvalidHostname(raw)
    return host


def base_domain() -> str:
    raw = str(getattr(settings, "WHITE_LABEL_BASE_DOMAIN", "") or "")
    return normalize(raw) if raw else ""


def platform_hosts() -> frozenset[str]:
    hosts = {normalize(h) for h in getattr(settings, "WHITE_LABEL_PLATFORM_HOSTS", []) if h}
    if base_domain():
        hosts.add(base_domain())
    return frozenset(hosts)


def reserved_labels() -> frozenset[str]:
    return frozenset(getattr(settings, "WHITE_LABEL_RESERVED_LABELS", []))


def is_platform_namespace(host: str) -> bool:
    """A platform host, or any name under the base domain: never registrable as a custom domain."""
    base = base_domain()
    return host in platform_hosts() or (bool(base) and (host == base or host.endswith("." + base)))


@dataclass(frozen=True)
class HostMatch:
    kind: str  # "platform" | "subdomain" | "custom"
    school_id: uuid.UUID | None


def _cache_key(host: str) -> str:
    return "branding:host:" + hashlib.sha256(host.encode()).hexdigest()


def forget(hostname: str) -> None:
    cache.delete(_cache_key(hostname))


def _custom_school(host: str) -> str:
    cached = cache.get(_cache_key(host))
    if cached is not None:
        return str(cached)
    with db_context.system_context("branding_host_lookup"):
        school_id = (
            SchoolDomain.objects.filter(hostname=host, status=DomainStatus.VERIFIED)
            .values_list("school_id", flat=True)
            .first()
        )
    value = str(school_id) if school_id else _MISS
    cache.set(_cache_key(host), value, CACHE_SECONDS if school_id else MISS_CACHE_SECONDS)
    return value


def classify(raw_host: str, *, allow_port: bool = False) -> HostMatch | None:
    try:
        host = normalize(raw_host, allow_port=allow_port)
    except InvalidHostname:
        return None
    if host in platform_hosts():
        return HostMatch("platform", None)
    base = base_domain()
    if base and host.endswith("." + base):
        label = host[: -len(base) - 1]
        if "." in label or label in reserved_labels():
            return None
        school_id = School.objects.filter(code=label, is_active=True).values_list("id", flat=True).first()
        return HostMatch("subdomain", school_id) if school_id else None
    found = _custom_school(host)
    if found == _MISS:
        return None
    if not School.objects.filter(pk=found, is_active=True).exists():
        return None
    return HostMatch("custom", uuid.UUID(found))


def school_for_request_host(request_host: str) -> uuid.UUID | None:
    """The school a request's own Host belongs to (already validated by ``ALLOWED_HOSTS``), else None."""
    match = classify(request_host, allow_port=True)
    return match.school_id if match is not None else None
