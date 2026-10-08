"""Phone number normalisation to E.164.

Deliberately small: separators are removed, a leading ``00`` becomes ``+``, and a national number of the
configured length gets the default country code (``PHONE_DEFAULT_COUNTRY_CODE``, India by default, as the
client assumes). Anything else must already be international. Numbers are not validated against
numbering plans; a dedicated library can replace this function without changing callers.
"""

from __future__ import annotations

import re

from django.conf import settings

_SEPARATORS = re.compile(r"[\s\-().]")
_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")


class InvalidPhone(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    value = _SEPARATORS.sub("", raw or "")
    if value.startswith("00"):
        value = "+" + value[2:]
    if not value.startswith("+"):
        national_length = int(getattr(settings, "PHONE_NATIONAL_NUMBER_LENGTH", 10))
        country = str(getattr(settings, "PHONE_DEFAULT_COUNTRY_CODE", "91"))
        if value.isdigit() and len(value) == national_length:
            value = f"+{country}{value}"
    if not _E164.fullmatch(value):
        raise InvalidPhone("Enter a valid mobile number.")
    return value


def mask_phone(phone: str) -> str:
    """For logs: keep only the last two digits."""
    return "*" * max(0, len(phone) - 2) + phone[-2:]
