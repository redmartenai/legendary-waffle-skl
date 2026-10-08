"""The client's IP address, as far as it can be trusted.

``X-Forwarded-For`` is written by every proxy in the chain, and the client can pre-fill it with anything.
Only the entries appended by proxies we operate are trustworthy, so the number of those proxies is
configured (``TRUSTED_PROXY_COUNT``) and the address is read that many hops from the right. With the
default of 0 the header is ignored and ``REMOTE_ADDR`` is used. Throttles and audit records both use
this function, so a spoofed header can neither dodge a rate limit nor forge an audit IP.
"""

from __future__ import annotations

import ipaddress

from django.conf import settings
from django.http import HttpRequest


def _valid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: HttpRequest) -> str | None:
    trusted = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0))
    if trusted > 0:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if len(hops) >= trusted:
            return _valid(hops[-trusted])
    return _valid(request.META.get("REMOTE_ADDR"))


def user_agent(request: HttpRequest, limit: int = 256) -> str:
    return str(request.META.get("HTTP_USER_AGENT", ""))[:limit]
