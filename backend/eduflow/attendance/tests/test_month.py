"""The per-student month view: day statuses, transfers, enrollment boundaries, and who may see it."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from eduflow.authz.models import MembershipRole, Role
from eduflow.authz.services import _set_grants, bump_rbac_version
from eduflow.people.models import Enrollment
from eduflow.people.services import transfer_enrollment

from .conftest import TODAY, YESTERDAY, absent, actor, take

pytestmark = pytest.mark.django_db


def _days(month):
    return {d["date"]: d["status"] for d in month["days"]}


def test_a_month_shows_marks_gaps_and_upcoming_days(world, class_a, school_day, api_as):
    admin = api_as(world.admin)
    take(admin, world.section_a, day=YESTERDAY, entries=absent(class_a.asha))
    take(admin, world.section_a, entries=absent(class_a.asha, status="late"))
    month = api_as(world.parent).get(f"students/{class_a.asha.pk}/attendance?month=2026-07")
    days = _days(month)
    assert month["month"] == "2026-07"
    assert len(days) == 31
    assert (days["2026-07-13"], days["2026-07-14"]) == ("absent", "late")
    assert days["2026-07-01"] == "not_marked"
    assert days["2026-07-15"] == days["2026-07-31"] == "upcoming"
    assert month["summary"] == {
        "present": 0,
        "absent": 1,
        "late": 1,
        "half_day": 0,
        "excused": 0,
        "not_marked": 12,
        "marked_days": 2,
    }


def test_the_default_month_is_the_current_one(world, class_a, school_day, api_as):
    assert api_as(world.student_member).get(f"students/{class_a.asha.pk}/attendance")["month"] == "2026-07"


def test_days_outside_enrollment_are_null(world, class_a, school_day, api_as):
    june = api_as(world.admin).get(f"students/{class_a.asha.pk}/attendance?month=2026-05")
    assert set(_days(june).values()) == {None}  # before the academic year
    Enrollment.objects.filter(student=class_a.chitra).update(status="withdrawn", end_date=YESTERDAY)
    days = _days(api_as(world.admin).get(f"students/{class_a.chitra.pk}/attendance?month=2026-07"))
    assert (days["2026-07-13"], days["2026-07-14"]) == ("not_marked", None)


def test_a_transfer_takes_each_days_status_from_that_days_section(world, class_a, school_day, api_as):
    admin = api_as(world.admin)
    before = YESTERDAY - datetime.timedelta(days=1)
    take(admin, world.section_a, day=before, entries=absent(class_a.bala))
    enrollment = Enrollment.objects.select_related("academic_year").get(student=class_a.bala)
    transfer_enrollment(actor(world.admin), enrollment, section_id=world.section_b.pk, date=YESTERDAY)
    take(admin, world.section_b, day=YESTERDAY, entries=absent(class_a.bala, status="excused"))
    days = _days(admin.get(f"students/{class_a.bala.pk}/attendance?month=2026-07"))
    assert (days[str(before)], days[str(YESTERDAY)], days[str(TODAY)]) == ("absent", "excused", "not_marked")


@pytest.mark.parametrize("month", ["2026-13", "07-2026", "2026-7", "x"])
def test_bad_months_are_refused(world, class_a, school_day, api_as, month):
    response = api_as(world.admin).get(f"students/{class_a.asha.pk}/attendance?month={month}", expect=400)
    assert "month" in response["error"]["fields"]


def test_who_sees_whose_month(world, other_world, class_a, school_day, api_as, make_member):
    parent, student, teacher = api_as(world.parent), api_as(world.student_member), api_as(world.teacher)
    parent.get(f"students/{class_a.asha.pk}/attendance")
    parent.get(f"students/{class_a.bala.pk}/attendance", expect=404)  # not their child
    student.get(f"students/{class_a.asha.pk}/attendance")
    student.get(f"students/{class_a.bala.pk}/attendance", expect=404)
    teacher.get(f"students/{class_a.bala.pk}/attendance")  # in a section they teach
    teacher.get(f"students/{world.other_student.pk}/attendance", expect=404)
    api_as(world.admin).get(f"students/{other_world.student.pk}/attendance", expect=404)
    # An accountant reads students school-wide but holds no attendance permission.
    api_as(make_member(world.school, roles=["accountant"])).get(
        f"students/{class_a.bala.pk}/attendance", expect=403
    )


def test_both_permissions_must_cover_the_student(world, class_a, school_day, api_as, make_member):
    clerk = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key="clerk", name="Clerk")
    _set_grants(role, {"student.read": ["school"], "attendance.read": ["self"]})
    MembershipRole.objects.create(school=world.school, membership=clerk, role=role)
    bump_rbac_version(world.school.pk)
    api_as(clerk).get(f"students/{class_a.bala.pk}/attendance", expect=404)


def test_month_queries_do_not_grow_with_the_records(world, class_a, school_day, api_as):
    admin = api_as(world.admin)
    path = f"students/{class_a.asha.pk}/attendance?month=2026-07"
    take(admin, world.section_a, day=YESTERDAY)
    admin.get(path)
    with CaptureQueriesContext(connection) as small:
        admin.get(path)
    for back in range(2, 10):
        take(admin, world.section_a, day=TODAY - datetime.timedelta(days=back))
    with CaptureQueriesContext(connection) as large:
        admin.get(path)
    assert len(large) == len(small)
