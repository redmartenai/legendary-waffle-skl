"""Console seed: dashboard. Runs last, so it tidies up after the other areas."""

import random
from datetime import date, time, timedelta

from apps.accounts.models import Membership, Role


def seed(cmd, school):
    _backfill_staff_attendance(cmd, school)


def _backfill_staff_attendance(cmd, school):
    """Staff created after the people seed (e.g. the operations bus crews) get a check-in history too."""
    from apps.staff.models import StaffAttendance

    have = set(StaffAttendance.objects.values_list("user_id", flat=True).distinct())
    users = {
        m.user_id
        for m in Membership.objects.filter(is_active=True).exclude(role__in=[Role.PARENT, Role.STUDENT, Role.PRINCIPAL])
        if m.user_id not in have
    }
    if not users:
        return
    rng = random.Random(26092026)
    today = cmd.today
    rows = []
    d = date(today.year, 4, 1)
    while d <= today:
        if d.weekday() != 6:
            for uid in users:
                late = rng.random() < 0.04
                rows.append(
                    StaffAttendance(
                        school=school,
                        user_id=uid,
                        date=d,
                        status=StaffAttendance.Status.LATE if late else StaffAttendance.Status.PRESENT,
                        check_in=time(8, rng.randint(16, 35)) if late else time(7, rng.randint(40, 59)),
                        source=StaffAttendance.Source.APP,
                    )
                )
        d += timedelta(days=1)
    StaffAttendance.objects.bulk_create(rows, batch_size=1000, ignore_conflicts=True)
