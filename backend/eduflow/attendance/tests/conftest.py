"""Attendance fixtures, on top of the Phase 3 ``world``.

Section A has three students (the world's signed-in student "Asha", plus Bala and Chitra); section B has
"Ravi". ``school_day`` freezes the clock on Tuesday 2026-07-14, 09:30 in the school's time zone
(Asia/Kolkata); clients must be created after it (``api_as``), since tokens are issued at the frozen time.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import pytest

from eduflow.authz.grants import Actor
from eduflow.people.models import Enrollment, Student

TODAY = datetime.date(2026, 7, 14)  # a Tuesday
YESTERDAY = TODAY - datetime.timedelta(days=1)
MORNING = datetime.datetime(2026, 7, 14, 4, 0, tzinfo=datetime.UTC)  # 09:30 in Asia/Kolkata


class Api:
    def __init__(self, client: Any) -> None:
        self.client = client

    def _check(self, response: Any, expect: int) -> Any:
        assert response.status_code == expect, (response.status_code, response.content)
        return response.json() if response.content else None

    def get(self, path: str, expect: int = 200) -> Any:
        return self._check(self.client.get(f"/api/v1/{path}"), expect)

    def post(self, path: str, body: Any = None, expect: int = 200, **headers: Any) -> Any:
        return self._check(self.client.post(f"/api/v1/{path}", body or {}, format="json", **headers), expect)


@pytest.fixture
def school_day(time_machine: Any) -> datetime.date:
    time_machine.move_to(MORNING)
    return TODAY


@pytest.fixture
def api_as(as_member: Any) -> Any:
    def _make(membership: Any) -> Api:
        return Api(as_member(membership))

    return _make


@dataclass
class ClassA:
    asha: Student
    bala: Student
    chitra: Student

    @property
    def ids(self) -> set[str]:
        return {str(s.pk) for s in (self.asha, self.bala, self.chitra)}


@pytest.fixture
def class_a(world: Any) -> ClassA:
    def enrol(admission: str, first: str, roll: str) -> Student:
        student = Student.objects.create(school=world.school, admission_number=admission, first_name=first)
        Enrollment.objects.create(
            school=world.school,
            student=student,
            academic_year=world.year,
            grade=world.grade,
            section=world.section_a,
            start_date=world.year.start_date,
            roll_number=roll,
        )
        return student

    Enrollment.objects.filter(pk=world.enrollment.pk).update(roll_number="1")
    return ClassA(world.student, enrol("S-3", "Bala", "2"), enrol("S-4", "Chitra", "3"))


def take(api: Api, section: Any, *, day: datetime.date | None = None, expect: int = 200, **body: Any) -> Any:
    """Submit a register: ``take(api, section, entries=[...], client_id=..., day=...)``."""
    headers = {k: body.pop(k) for k in [k for k in body if k.startswith("HTTP_")]}
    payload = {"entries": body.pop("entries", []), **body}
    if day is not None:
        payload["date"] = str(day)
    return api.post(f"classes/{section.pk}/attendance", payload, expect=expect, **headers)


def absent(*students: Any, status: str = "absent") -> list[dict[str, str]]:
    return [{"student_id": str(s.pk), "status": status} for s in students]


def actor(membership: Any) -> Actor:
    """The service-level caller for ``membership``, with its real grants."""
    from eduflow.authz.grants import compute_grants

    return Actor(
        user=membership.user,
        school=membership.school,
        membership=membership,
        grants=compute_grants(membership),
    )
