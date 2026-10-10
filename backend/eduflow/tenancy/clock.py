"""School-local time. Every "today", "late" and "overdue" rule is judged in the school's own time zone."""

from __future__ import annotations

import datetime
import zoneinfo
from typing import Any

from django.utils import timezone


def zone(school: Any) -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(school.timezone)


def now(school: Any) -> datetime.datetime:
    return timezone.now().astimezone(zone(school))


def today(school: Any) -> datetime.date:
    return now(school).date()


def local(moment: datetime.datetime, school: Any) -> datetime.datetime:
    return moment.astimezone(zone(school))
