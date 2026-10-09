"""Phase 5 fixtures: a timetable built through the API on top of the Phase 3 ``world``.

Reference week: Monday 2026-07-13 to Sunday 2026-07-19 (inside the world's 2026-27 academic year).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

import pytest

from eduflow.academics.models import Subject
from eduflow.people.models import StaffProfile, TeacherAssignment

MONDAY = datetime.date(2026, 7, 13)
TUESDAY = MONDAY + datetime.timedelta(days=1)


class Api:
    """A signed-in client with JSON helpers that assert the expected status."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def call(self, method: str, path: str, body: Any = None, expect: int = 200) -> Any:
        response = getattr(self.client, method)(f"/api/v1/{path}", body, format="json")
        assert response.status_code == expect, (response.status_code, response.content)
        return response.json() if response.content else None

    def post(self, path: str, body: Any = None, expect: int = 201) -> Any:
        return self.call("post", path, body or {}, expect)

    def patch(self, path: str, body: Any, expect: int = 200) -> Any:
        return self.call("patch", path, body, expect)

    def get(self, path: str, expect: int = 200) -> Any:
        response = self.client.get(f"/api/v1/{path}")
        assert response.status_code == expect, (response.status_code, response.content)
        return response.json()

    def delete(self, path: str, expect: int = 204) -> Any:
        return self.call("delete", path, None, expect)


@dataclass
class Plan:
    """A timetable under construction, with helpers for its periods and slots."""

    api: Api
    world: Any
    id: str
    periods: dict[int, str] = field(default_factory=dict)

    def period(self, number: int, start: str, end: str, *, is_break: bool = False) -> str:
        body = {
            "timetable_id": self.id,
            "number": number,
            "name": f"Period {number}",
            "start_time": start,
            "end_time": end,
            "is_break": is_break,
        }
        self.periods[number] = self.api.post("timetable-periods", body)["id"]
        return self.periods[number]

    def slot(
        self,
        number: int,
        weekday: int = 1,
        *,
        section: Any = None,
        assignment: Any = None,
        room: Any = None,
        expect: int = 201,
        **extra: Any,
    ) -> Any:
        section = section or self.world.section_a
        if assignment is None and extra.get("kind") != "activity":
            assignment = self.world.assignment
        body = {
            "timetable_id": self.id,
            "period_id": self.periods[number],
            "weekday": weekday,
            "section_id": str(section.pk),
            **({"assignment_id": str(assignment.pk)} if assignment is not None else {}),
            **({"room_id": str(room.pk)} if room is not None else {}),
            **extra,
        }
        return self.api.post("timetable-slots", body, expect=expect)

    def publish(self, expect: int = 200) -> Any:
        return self.api.post(f"timetables/{self.id}/publish", expect=expect)

    def archive(self, expect: int = 200) -> Any:
        return self.api.post(f"timetables/{self.id}/archive", expect=expect)


@pytest.fixture
def api_as(as_member: Any) -> Any:
    def _make(membership: Any) -> Api:
        return Api(as_member(membership))

    return _make


@pytest.fixture
def admin_api(world: Any, api_as: Any) -> Api:
    api: Api = api_as(world.admin)
    return api


@pytest.fixture
def new_plan(world: Any, admin_api: Api) -> Any:
    """``new_plan(name, **dates)`` -> an empty draft timetable of the world's year."""

    def _make(name: str = "Main", *, api: Api | None = None, **extra: Any) -> Plan:
        api = api or admin_api
        body = {"academic_year_id": str(world.year.pk), "name": name, **extra}
        return Plan(api, world, api.post("timetables", body)["id"])

    return _make


@pytest.fixture
def plan(new_plan: Any) -> Plan:
    """A draft with three periods (period 2 is a break)."""
    built: Plan = new_plan()
    built.period(1, "09:00", "09:45")
    built.period(2, "09:45", "10:00", is_break=True)
    built.period(3, "10:00", "10:45")
    return built


@pytest.fixture
def second_teacher(world: Any, make_member: Any) -> Any:
    """Another teacher, assigned to section B for Mathematics and to section A for Science."""
    member = make_member(world.school, roles=["teacher"])
    staff = StaffProfile.objects.create(school=world.school, membership=member, employee_id="T-2")
    science = Subject.objects.create(school=world.school, name="Science", code="science")
    maths_b = TeacherAssignment.objects.create(
        school=world.school,
        staff=staff,
        academic_year=world.year,
        section=world.section_b,
        subject=world.subject,
    )
    science_a = TeacherAssignment.objects.create(
        school=world.school, staff=staff, academic_year=world.year, section=world.section_a, subject=science
    )

    @dataclass
    class Teacher:
        member: Any
        staff: StaffProfile
        maths_b: TeacherAssignment
        science_a: TeacherAssignment

    return Teacher(member, staff, maths_b, science_a)


def week(api: Api, path: str, start: datetime.date = MONDAY, days: int = 7) -> list[dict[str, Any]]:
    end = start + datetime.timedelta(days=days - 1)
    data = api.get(f"{path}?date_from={start}&date_to={end}")
    entries: list[dict[str, Any]] = data["entries"]
    return entries
