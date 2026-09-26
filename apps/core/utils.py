import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.utils import timezone

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(raw, default_country_code: str = "91") -> str:
    """Normalize Indian-style input (98765 43210, 098765..., 91987...) to E.164."""
    if raw is None or not str(raw).strip():
        raise ValueError("Enter your mobile number.")
    text = str(raw).strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        candidate = f"+{digits}"
    elif len(digits) == 10:
        candidate = f"+{default_country_code}{digits}"
    elif len(digits) == 11 and digits.startswith("0"):
        candidate = f"+{default_country_code}{digits[1:]}"
    elif len(digits) == 12 and digits.startswith(default_country_code):
        candidate = f"+{digits}"
    else:
        candidate = f"+{digits}"
    if not _E164.match(candidate):
        raise ValueError("Enter a valid mobile number.")
    return candidate


def mask_phone(phone: str) -> str:
    return f"{phone[:3]} ••••• {phone[-3:]}" if phone and len(phone) > 6 else "•••"


def school_tz(school) -> ZoneInfo:
    return ZoneInfo(getattr(school, "timezone", None) or "Asia/Kolkata")


def school_now(school) -> datetime:
    return timezone.now().astimezone(school_tz(school))


def school_today(school) -> date:
    return school_now(school).date()


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def time_in_window(moment: time, start: time, end: time) -> bool:
    """True when ``moment`` falls in [start, end); supports overnight windows like 21:00–07:00."""
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "smt", "shri"}


def initials(name: str) -> str:
    # "Dr. Anita Rao" -> "AR": honorifics aren't part of the initials.
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p and p[0].isalpha() and p.lower().rstrip(".") not in HONORIFICS]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def first_name(name: str) -> str:
    parts = (name or "").strip().split()
    if not parts:
        return ""
    # Honorifics and initials ("S. Krishnan", "Dr. Rao") read better with the next part.
    if len(parts) > 1 and (parts[0].endswith(".") or len(parts[0]) == 1):
        return parts[1]
    return parts[0]


def iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value.isoformat()
