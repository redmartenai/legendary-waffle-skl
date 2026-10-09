"""Proof that a school controls a custom domain: a DNS TXT record (ADR-029).

The owner publishes::

    _eduflow-challenge.<hostname>  TXT  "eduflow-domain-verification=<token>"

and asks EduFlow to check. ``DOMAIN_VERIFIER`` selects how:

======================  ================================================================================
Verifier                Use
======================  ================================================================================
``DisabledVerifier``    Default. Automatic checks answer ``503``; the platform can verify by hand after
                        checking DNS out of band (an audited action).
``MemoryVerifier``      Tests: records are set in ``MemoryVerifier.records``.
``DnsOverHttpsVerifier`` Production option without extra dependencies: a JSON DNS-over-HTTPS query to the
                        fixed ``DOMAIN_VERIFICATION_DOH_URL`` (https only). Only the challenge name is sent.
======================  ================================================================================
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import ClassVar, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

CHALLENGE_PREFIX = "_eduflow-challenge."


def challenge_name(hostname: str) -> str:
    return CHALLENGE_PREFIX + hostname


def challenge_value(token: str) -> str:
    return f"eduflow-domain-verification={token}"


class VerificationUnavailable(Exception):
    """No automatic verification is configured, or the resolver could not be reached."""


class DomainVerifier(Protocol):
    enabled: bool

    def txt_records(self, name: str) -> list[str]: ...


class DisabledVerifier:
    enabled = False

    def txt_records(self, name: str) -> list[str]:
        raise VerificationUnavailable()


class MemoryVerifier:
    enabled = True
    records: ClassVar[dict[str, list[str]]] = {}

    def txt_records(self, name: str) -> list[str]:
        return list(self.records.get(name, []))


class DnsOverHttpsVerifier:
    enabled = True
    TIMEOUT_SECONDS = 5

    def txt_records(self, name: str) -> list[str]:
        base = str(settings.DOMAIN_VERIFICATION_DOH_URL)
        if not base.startswith("https://"):
            raise VerificationUnavailable()
        url = f"{base}?{urllib.parse.urlencode({'name': name, 'type': 'TXT'})}"
        request = urllib.request.Request(url, headers={"Accept": "application/dns-json"})  # noqa: S310
        try:
            # The URL is the fixed, operator-configured https resolver; only the query string varies.
            with urllib.request.urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:  # noqa: S310  # nosec B310
                payload = json.loads(response.read(65536))
        except (OSError, ValueError) as exc:
            raise VerificationUnavailable() from exc
        answers = (payload.get("Answer") or []) if isinstance(payload, dict) else []
        records = []
        for answer in answers:
            if isinstance(answer, dict) and answer.get("type") == 16:  # TXT
                # TXT data arrives quoted, possibly split into several strings: "abc" "def".
                data = str(answer.get("data", ""))
                records.append("".join(part for part in data.split('"') if part.strip()))
        return records


def verifier() -> DomainVerifier:
    instance: DomainVerifier = import_string(settings.DOMAIN_VERIFIER)()
    return instance


def has_challenge(hostname: str, token: str) -> bool:
    return challenge_value(token) in verifier().txt_records(challenge_name(hostname))
