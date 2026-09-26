"""Console seed: people (Students, Student profile, Admissions, Staff).

- A guardian for every child (brothers and sisters share one), phones ``cmd._phone(3000..3845)``.
- When each child joined: admission dates behind the "New this term" filter.
- Staff: HR profiles for everyone, the term's check-in register (``StaffAttendance``) and two open positions.
- Admissions 2027–28: the cycle, seats by grade and 214 families from enquiry to admission, with their history.
  Seeded admissions have no Student row (other seeds treat every Student as enrolled this year); the console's
  Admit action creates one, inactive until the session starts.
  Applicants' phones are ``cmd._phone(3846..3999)``; sibling applications reuse the family's phone.

Idempotent: re-running updates guardians in place and rebuilds attendance, vacancies and admissions.
"""

import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.contrib.auth.hashers import make_password

from apps.academics.models import Student, StudentGuardian
from apps.accounts.models import Membership, Role, User

GUARDIAN_PHONES = range(3000, 3846)
APPLICANT_PHONES = range(3846, 4000)

FATHERS = [
    "Rajesh", "Sanjay", "Suresh", "Srinivas", "Gopal", "Ramesh", "Arindam", "Vikas", "Rohit", "Mahesh", "Nitin", "Karan",
    "Deepak", "Anil", "Prakash", "Manoj", "Harish", "Venkat", "Ashok", "Sunil", "Ajay", "Naveen", "Ravi", "Kiran", "Vinod",
]
MOTHERS = [
    "Lakshmi", "Meera", "Pooja", "Nikita", "Swati", "Neha", "Maya", "Ritu", "Anjali", "Kavita", "Divya", "Asha", "Rekha",
    "Radhika", "Geeta", "Nandini", "Sneha", "Aarti", "Shweta", "Padma", "Uma", "Jyoti",
]
MUSLIM_LAST = {"Khan", "Qureshi", "Siddiqui", "Sheikh", "Sayed"}
MUSLIM_FATHERS = ["Imran", "Farhan", "Salman", "Irfan", "Arif"]
MUSLIM_MOTHERS = ["Fatima", "Ayesha", "Sana", "Nazia", "Rukhsar"]


def seed(cmd, school):
    rng = random.Random(260926)
    today = cmd.today
    guardians = _guardians(cmd, school, rng)
    _joined_dates(cmd, school, rng, today)
    _staff(cmd, school, rng, today)
    _admissions(cmd, school, rng, today, guardians)


# ---------------------------------------------------------------------------------------------- guardians


def _parent_name(rng, last: str, mother: bool) -> str:
    if last in MUSLIM_LAST:
        return f"{rng.choice(MUSLIM_MOTHERS if mother else MUSLIM_FATHERS)} {last}"
    return f"{rng.choice(MOTHERS if mother else FATHERS)} {last}"


def _guardians(cmd, school, rng) -> dict:
    """Give every child without one a primary guardian. Returns student id -> guardian user."""
    linked = set(StudentGuardian.objects.values_list("student_id", flat=True))
    students = list(Student.objects.filter(is_active=True).select_related("class_group").order_by("admission_no"))
    families: dict[str, list] = defaultdict(list)  # last name -> [(guardian phone, name, relationship, {grades})]
    plan = []  # (student, phone, name, relationship)
    phones = iter(GUARDIAN_PHONES)
    left = len(GUARDIAN_PHONES)
    todo = [s for s in students if s.id not in linked]
    for index, s in enumerate(todo):
        last = s.full_name.split()[-1]
        remaining = len(todo) - index
        # About a quarter of families have two children here; share more once phones run short.
        candidates = [f for f in families[last] if s.class_group.grade not in f[3] and len(f[3]) < 3]
        share = candidates and (rng.random() < 0.26 or left < remaining)
        if s.full_name == "Ishita Kapoor" or (s.class_group.short_label == "4-A" and last == "Kapoor"):
            share = False
        if share:
            family = rng.choice(candidates)
            family[3].add(s.class_group.grade)
            plan.append((s, family[0], family[1], family[2]))
            continue
        mother = rng.random() < 0.45
        name = _parent_name(rng, last, mother)
        if s.class_group.short_label == "4-A" and last == "Kapoor":
            name, mother = "Neha Kapoor", True  # the sibling on Ishita Kapoor's admission
        phone = cmd._phone(next(phones))
        left -= 1
        family = (phone, name, "mother" if mother else "father", {s.class_group.grade})
        families[last].append(family)
        plan.append((s, phone, name, family[2]))

    wanted = {phone: name for _s, phone, name, _r in plan}
    existing = {u.phone: u for u in User.objects.filter(phone__in=wanted)}
    unusable = make_password(None)
    fresh = [User(phone=p, full_name=n, password=unusable) for p, n in wanted.items() if p not in existing]
    User.objects.bulk_create(fresh, batch_size=500)
    users = {u.phone: u for u in User.objects.filter(phone__in=wanted)}
    for phone, name in wanted.items():
        if users[phone].full_name != name:
            User.objects.filter(pk=users[phone].pk).update(full_name=name)
    have = set(Membership.objects.filter(role=Role.PARENT, user__phone__in=wanted).values_list("user_id", flat=True))
    Membership.objects.bulk_create(
        [Membership(school=school, user=u, role=Role.PARENT, title="Parent") for u in users.values() if u.id not in have], batch_size=500
    )
    StudentGuardian.objects.bulk_create(
        [StudentGuardian(school=school, student=s, user=users[p], relationship=rel, is_primary=True) for s, p, _n, rel in plan],
        batch_size=500,
    )
    out = {}
    for link in StudentGuardian.objects.filter(is_primary=True).select_related("user"):
        out.setdefault(link.student_id, link.user)
    return out


# ---------------------------------------------------------------------------------------------- joined dates


def _adm_year(student) -> int | None:
    parts = student.admission_no.split("/")
    return int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else None


def _joined_dates(cmd, school, rng, today):
    """``created_at`` is when the child joined: June of the admission year, and 23 who joined this term."""
    tz = cmd.tz
    term_start = date(2026, 4, 1)
    students = list(Student.objects.filter(is_active=True).select_related("class_group"))
    this_year = [s for s in students if _adm_year(s) == 2026 and s.full_name not in ("Aarav Sharma", "Diya Sharma")]
    rng.shuffle(this_year)
    new_ids = {s.id for s in this_year[:23]}
    for s in students:
        year = _adm_year(s) or 2020
        if s.id in new_ids:
            joined = term_start + timedelta(days=rng.randint(0, (today - term_start).days - 5))
        elif year >= 2026:
            joined = date(2026, 3, rng.randint(16, 30))  # admitted for this year before term began
        else:
            joined = date(year, 6, rng.randint(1, 12))
        s.created_at = datetime.combine(joined, time(10, rng.randint(0, 59)), tzinfo=tz)
    Student.objects.bulk_update(students, ["created_at"], batch_size=500)


# ---------------------------------------------------------------------------------------------- staff


def _staff(cmd, school, rng, today):
    from apps.academics.models import Subject, TeachingAssignment
    from apps.staff.models import StaffAttendance, StaffLeave, StaffProfile, Vacancy

    tz = cmd.tz
    members = list(
        Membership.objects.filter(is_active=True)
        .exclude(role__in=[Role.PARENT, Role.STUDENT, Role.PRINCIPAL])
        .select_related("user")
        .order_by("user__phone")
    )
    people = {}
    for m in members:
        people.setdefault(m.user_id, m)

    # --- HR profiles for everyone the design seed didn't give one ---
    have = set(StaffProfile.objects.values_list("user_id", flat=True))
    subjects = {}
    for a in TeachingAssignment.objects.select_related("subject").order_by("created_at"):
        subjects.setdefault(a.teacher_id, a.subject.name)
    t_no, s_no, profiles = 200, 300, []
    for uid, m in people.items():
        if uid in have:
            continue
        teacher = m.role == Role.TEACHER
        if teacher:
            t_no += 1
        else:
            s_no += 1
        profiles.append(
            StaffProfile(
                school=school,
                user=m.user,
                employee_id=f"SPS-T-{t_no:04d}" if teacher else f"SPS-S-{s_no:04d}",
                designation=subjects.get(uid, "Teacher") if teacher else (m.title or m.get_role_display()),
                joined_on=date(2012 + rng.randint(0, 13), 6, 1),
            )
        )
    StaffProfile.objects.bulk_create(profiles)

    # --- The check-in register from the start of the term to today ---
    StaffAttendance.objects.all().delete()
    start = date(2026, 4, 1)
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    days = [d for d in days if d.weekday() != 6]
    leave = defaultdict(set)
    for lv in StaffLeave.objects.filter(status=StaffLeave.Status.APPROVED, to_date__gte=start, from_date__lte=today):
        d = lv.from_date
        while d <= lv.to_date:
            leave[lv.user_id].add(d)
            d += timedelta(days=1)
    # Named teachers' check-ins this morning, as the Staff page shows them.
    fixed_today = {"Priya Menon": time(7, 48), "Joseph Thomas": time(7, 41), "Nikhil Rao": time(7, 55), "Deepa Iyer": time(7, 39), "Farah Khan": time(7, 51)}
    by_name = {m.user.full_name: uid for uid, m in people.items()}
    fixed_today = {by_name[n]: t for n, t in fixed_today.items() if n in by_name}
    late_today = [uid for uid, m in people.items() if uid not in fixed_today and today not in leave[uid]]
    rng.shuffle(late_today)
    late_today = set(late_today[:3])
    rows = []
    for uid, m in people.items():
        rate = rng.uniform(0.004, 0.035)  # this person's unexplained absences
        for d in days:
            if d in leave[uid]:
                rows.append(StaffAttendance(school=school, user_id=uid, date=d, status="leave", source="office"))
                continue
            r = rng.random()
            if d == today:
                status = "late" if uid in late_today else "present"
            else:
                status = "absent" if r < rate else "late" if r < rate + 0.03 else "present"
            if status == "absent":
                rows.append(StaffAttendance(school=school, user_id=uid, date=d, status="absent", source="office"))
                continue
            if d == today and uid in fixed_today:
                check_in = fixed_today[uid]
            elif status == "late":
                check_in = time(8, rng.randint(16, 34))
            else:
                check_in = time(7, rng.randint(22, 59)) if rng.random() < 0.92 else time(8, rng.randint(0, 2))
            check_out = None if d == today else time(15, rng.randint(30, 59)) if rng.random() < 0.7 else time(16, rng.randint(0, 45))
            source = "biometric" if rng.random() < 0.9 else "app"
            rows.append(StaffAttendance(school=school, user_id=uid, date=d, status=status, check_in=check_in, check_out=check_out, source=source))
    StaffAttendance.objects.bulk_create(rows, batch_size=2000)

    # --- Open positions ---
    Vacancy.objects.all().delete()
    hindi = Subject.objects.filter(code="HIN").first()
    Vacancy.objects.create(title="Physics (PGT)", kind="teaching", positions=1, applicants=12, opened_on=today - timedelta(days=24), closes_on=today + timedelta(days=12), note="Grades 11–12")
    Vacancy.objects.create(title="Primary Hindi", kind="teaching", subject=hindi, positions=1, applicants=7, opened_on=today - timedelta(days=10), closes_on=today + timedelta(days=20), note="Grades 1–5")
    Vacancy.objects.create(title="Lab assistant", kind="support", positions=1, applicants=9, status="filled", opened_on=date(2026, 6, 2), closes_on=date(2026, 7, 15))
    _ = tz


# ---------------------------------------------------------------------------------------------- admissions


# Cards the design shows at the top of each column: name, grade, source, guardian, days before today, extras.
VISIBLE = {
    "enquiry": [
        ("Myra Bhatt", "Nursery", "website", "Karan Bhatt", 1, {"follow_up": "call_back", "follow_up_in": 0}),
        ("Vivaan Chopra", "6", "referral", "Pooja Chopra", 2, {"follow_up": "tour"}),
        ("Sara D'Souza", "2", "walk_in", "Allan D'Souza", 4, {"follow_up": "prospectus", "follow_up_in": -3}),
    ],
    "application": [
        ("Kabir Mehta", "3", "social", "Ritu Mehta", 3, {}),
        ("Anvi Shetty", "Nursery", "website", "Deepak Shetty", 5, {"form_fee_paid": True}),
        ("Rudra Iyer", "9", "referral", "Lakshmi Iyer", 7, {"documents_pending": "Report card"}),
    ],
    "assessment": [
        ("Aanya Gupta", "1", "walk_in", "Nitin Gupta", 0, {"slot": time(10, 0)}),
        ("Dhruv Nambiar", "6", "website", "Anjali Nambiar", 6, {"score": 88}),
    ],
    "documents": [
        ("Samaira Khan", "11", "website", "Imran Khan", 8, {"documents_pending": "TC"}),
    ],
    "offer": [
        ("Arnav Pillai", "1", "referral", "Maya Pillai", 7, {"reply_in": 3}),
        ("Kiara Bansal", "Nursery", "social", "Rohit Bansal", 11, {"accepted": True, "fee_in": 4}),
    ],
    "admitted": [
        ("Ira Deshpande", "Nursery", "website", "Swati Deshpande", 4, {"receipt": "SPS-R-25890"}),
        ("Yash Rathore", "9", "walk_in", "Mahesh Rathore", 6, {"receipt": "SPS-R-25871"}),
    ],
}
# The two applications the design seed puts in the principal's in-tray (Ishita Kapoor, and one renamed Reyansh Arora).
DESIGN_WAITING = ("APP-27-0412", "APP-27-0398")
# Children on the board in each stage (open), and how many dropped out at each stage. The operations seed adds
# one more in Documents (Advait Joshi, a referral), which brings the column to the design's 9.
OPEN = {"enquiry": 47, "application": 18, "assessment": 14, "documents": 8, "offer": 11, "admitted": 41}
CLOSED = {"enquiry": 71, "application": 3}
SOURCES = {"website": 82, "walk_in": 54, "referral": 45, "social": 32}
# Admitted children by seat row (Nursery 19 · Grade 1 8 · Grades 2–5 4 · Grade 6 5 · Grades 7–10 3 · Grade 11 2).
SEATS = [
    ("Nursery", ["Nursery"], 60, 19),
    ("Grade 1", ["1"], 20, 8),
    ("Grades 2–5", ["2", "3", "4", "5"], 12, 4),
    ("Grade 6", ["6"], 10, 5),
    ("Grades 7–10", ["7", "8", "9", "10"], 14, 3),
    ("Grade 11", ["11"], 24, 2),
]
CHILD_FIRST = [
    "Aarush", "Advika", "Aarna", "Atharv", "Avyaan", "Hrishaan", "Inaaya", "Kavya", "Kiaan", "Mishka", "Nirvaan", "Ojas",
    "Prisha", "Reeva", "Rian", "Saisha", "Shanaya", "Tanvi", "Vedant", "Viaan", "Aadvik", "Anaisha", "Darsh", "Eshaan",
    "Hiya", "Jiya", "Laksh", "Myra", "Navya", "Parth", "Ruhi", "Samar", "Siya", "Tiya", "Veer", "Yuvaan", "Zoya", "Arya",
]
CHILD_LAST = [
    "Agarwal", "Bhat", "Chawla", "Deshmukh", "Gowda", "Hegde", "Jain", "Kamath", "Lal", "Mathur", "Naidu", "Oberoi",
    "Prabhu", "Rastogi", "Saxena", "Thakur", "Upadhyay", "Venkatesh", "Wadhwa", "Yadav", "Kulkarni", "Shetty", "Rao",
]


def _admissions(cmd, school, rng, today, guardians):
    from apps.admissions.models import AdmissionCycle, Application, ApplicationEvent, SeatPlan
    from apps.approvals.models import ApprovalRequest

    tz = cmd.tz
    year = "2027–28"
    admitted_by = User.objects.filter(phone=cmd._phone(43)).first() or cmd.principal  # Nandini Shetty, admissions

    # --- start again, keeping the two applications already waiting in the principal's in-tray ---
    waiting_ids = set(ApprovalRequest.objects.filter(kind="admission").values_list("target_id", flat=True))
    old = Application.objects.exclude(id__in=waiting_ids)
    _drop_students(list(old.exclude(student=None).values_list("student_id", flat=True)))
    old.delete()
    ApplicationEvent.objects.all().delete()
    AdmissionCycle.objects.all().delete()

    cycle = AdmissionCycle.objects.create(
        academic_year=year,
        enquiries_open_on=date(2026, 8, 1),
        applications_close_on=date(2026, 12, 15),
        session_starts_on=date(2027, 4, 1),
        offer_rounds=[{"name": "Offer round 1", "on": (today - timedelta(days=11)).isoformat()}, {"name": "Offer round 2", "on": "2026-10-20"}],
    )
    for order, (label, grades, seats, _admitted) in enumerate(SEATS):
        SeatPlan.objects.create(cycle=cycle, label=label, grades=grades, seats=seats, order=order)

    # The next assessment day is the coming Thursday; 11 children have slots on it.
    assess_day = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
    next_sat = today + timedelta(days=(5 - today.weekday()) % 7 or 7)

    sources = [s for s, n in SOURCES.items() for _ in range(n)]
    for rows in VISIBLE.values():
        for _name, _grade, source, *_rest in rows:
            sources.remove(source)
    sources.remove("walk_in")  # Ishita Kapoor
    sources.remove("referral")  # Reyansh Arora
    rng.shuffle(sources)

    # Admitted children per seat row, less the ones the design shows by name.
    per_band = [n for _label, _grades, _s, n in SEATS]
    for _name, grade, *_rest in VISIBLE["admitted"]:
        band = next(i for i, row in enumerate(SEATS) if grade in row[1])
        per_band[band] -= 1
    admitted_grades = [rng.choice(SEATS[i][1]) for i, n in enumerate(per_band) for _ in range(n)]
    rng.shuffle(admitted_grades)

    used_names = set(Application.objects.values_list("child_name", flat=True)) | {r[0] for rows in VISIBLE.values() for r in rows} | {"Reyansh Arora"}
    applicant_phones = iter(APPLICANT_PHONES)
    siblings = list(Student.objects.filter(is_active=True, class_group__grade__in=["1", "2", "3", "4", "5", "6", "7", "8"]).order_by("admission_no"))
    rng.shuffle(siblings)
    sibling_iter = iter(siblings)

    def at(day, h=10, m=0):
        return datetime.combine(day, time(h, m), tzinfo=tz)

    def child():
        while True:
            name = f"{rng.choice(CHILD_FIRST)} {rng.choice(CHILD_LAST)}"
            if name not in used_names:
                used_names.add(name)
                return name

    def grade_pick():
        return rng.choices(["Nursery", "1", "2", "3", "4", "5", "6", "7", "8", "9", "11"], weights=[30, 14, 5, 5, 4, 4, 10, 4, 3, 5, 11])[0]

    counter = [100]  # APP-27-0101 onwards: clear of the in-tray numbers (0398, 0412, 0433)

    def make(name, grade, source, stage, days_ago, guardian=None, closed=False, extra=None):
        extra = extra or {}
        counter[0] += 1
        sibling, phone = None, None
        if guardian is None and rng.random() < 0.3:
            phone = None
        else:
            n = next(applicant_phones, None)
            phone = cmd._phone(n) if n is not None else None
        if phone is None:
            # A brother or sister already studies here: the family applies with the same phone.
            sibling = next(sibling_iter)
            g = guardians[sibling.id]
            guardian, phone = g.full_name, g.phone
            name = f"{name.split()[0]} {sibling.full_name.split()[-1]}"
        if guardian is None:
            last = name.split()[-1]
            guardian = _parent_name(rng, last, rng.random() < 0.5)
        idx = Application.ORDER.index(stage)
        moved = today - timedelta(days=days_ago)
        enquired = max(cycle.enquiries_open_on, moved - timedelta(days=rng.randint(4, 12) * idx + rng.randint(0, 5)))
        app = Application(
            application_no=f"APP-27-{counter[0]:04d}",
            child_name=name,
            grade=grade,
            academic_year=year,
            guardian_name=guardian,
            guardian_phone=phone,
            sibling=sibling,
            stage=stage,
            stage_changed_at=at(moved, rng.randint(9, 16), rng.randint(0, 59)),
            source=source,
            enquired_on=enquired,
            closed=closed,
            closed_reason=rng.choice(["Chose another school", "No response", "Moved city", "Fee concerns"]) if closed else "",
            form_fee_paid=idx >= 1 and (idx >= 2 or extra.get("form_fee_paid", rng.random() < 0.5)),
            documents_pending=extra.get("documents_pending", ""),
        )
        if stage == "enquiry" and not closed:
            kind = extra.get("follow_up") or rng.choice(["call_back", "tour", "prospectus", "call_back"])
            app.follow_up = kind
            if kind == "tour":
                app.follow_up_on = next_sat
            elif kind == "call_back":
                app.follow_up_on = today + timedelta(days=extra.get("follow_up_in", rng.randint(0, 3)))
            else:
                app.follow_up_on = today + timedelta(days=extra.get("follow_up_in", -rng.randint(1, 6)))
        if idx >= 2:
            booked = moved if stage == "assessment" else moved - timedelta(days=rng.randint(6, 14))
            if stage == "assessment" and "score" not in extra and extra.get("slot") is not None:
                app.assessment_at = at(assess_day, extra["slot"].hour, extra["slot"].minute)
            elif stage == "assessment" and "score" not in extra:
                app.assessment_at = at(assess_day, 9 + rng.randint(0, 3), rng.choice([0, 30]))
            else:
                app.assessment_at = at(booked, 9 + rng.randint(0, 3), rng.choice([0, 30]))
                app.assessment_score = extra.get("score", rng.randint(62, 96))
        if idx >= 3:
            app.documents_verified = stage != "documents" or not app.documents_pending
            app.interaction_on = moved - timedelta(days=rng.randint(1, 4))
        if idx >= 4:
            app.status = Application.Status.OFFERED
            app.offer_made_on = moved
            app.offer_reply_by = today + timedelta(days=extra.get("reply_in", rng.randint(1, 7))) if stage == "offer" else moved + timedelta(days=7)
            if extra.get("accepted") or stage == "admitted" or (stage == "offer" and "reply_in" not in extra and rng.random() < 0.4):
                app.offer_accepted_on = moved + timedelta(days=rng.randint(1, 3)) if stage == "admitted" else moved + timedelta(days=2)
                app.fee_due_on = today + timedelta(days=extra.get("fee_in", rng.randint(2, 9))) if stage == "offer" else moved
        if stage == "admitted":
            app.fee_receipt_no = extra.get("receipt") or f"SPS-R-{25800 + counter[0]:05d}"
            app.admitted_on = moved
        app.save()
        _history(app, admitted_by, at)
        return app

    # --- the two applications already in the principal's in-tray: Documents, verified, awaiting approval ---
    for app in Application.objects.filter(id__in=waiting_ids, application_no__in=DESIGN_WAITING):
        ishita = app.child_name == "Ishita Kapoor"
        if not ishita:
            app.child_name = "Reyansh Arora"
        g = guardians.get(app.sibling_id) if app.sibling_id else None
        app.guardian_name = g.full_name if g else "Vikas Arora"
        app.guardian_phone = g.phone if g else cmd._phone(next(applicant_phones))
        app.source = "walk_in" if ishita else "referral"
        app.enquired_on = today - timedelta(days=26 if ishita else 30)
        app.stage_changed_at = at(today - timedelta(days=1 if ishita else 2), 11, 20)
        app.form_fee_paid = True
        app.assessment_at = at(today - timedelta(days=12 if ishita else 16), 10)
        app.assessment_score = 91 if ishita else 84
        app.closed, app.closed_reason = False, ""
        if app.status == Application.Status.APPLIED:
            app.stage = "documents"
        app.save()
        _history(app, admitted_by, at)

    # --- the cards the design shows, then everyone else in each column ---
    for stage, rows in VISIBLE.items():
        for name, grade, source, guardian, days_ago, extra in rows:
            make(name, grade, source, stage, days_ago, guardian=guardian, extra=extra)
    for stage, total in OPEN.items():
        shown = len(VISIBLE[stage]) + (2 if stage == "documents" else 0)
        for i in range(total - shown):
            if stage == "admitted":
                grade = admitted_grades.pop()
            else:
                grade = grade_pick()
            extra = {}
            if stage == "assessment":
                extra = {"score": rng.randint(66, 95)} if i < 2 else {"slot": time(9 + (i % 4), 30 if i % 2 else 0)}
            if stage == "application" and rng.random() < 0.3:
                extra = {"documents_pending": rng.choice(["Report card", "Birth certificate"])}
            if stage == "documents":
                extra = {"documents_pending": rng.choice(["TC", "Report card", "Address proof", "Birth certificate"])}
            days_ago = len(VISIBLE[stage]) + i + rng.randint(1, 6) if stage != "enquiry" else rng.randint(3, 50)
            make(child(), grade, sources.pop(), stage, days_ago, extra=extra)
    for stage, total in CLOSED.items():
        for _ in range(total):
            make(child(), grade_pick(), sources.pop(), stage, rng.randint(5, 50), closed=True)


def _history(app, office, at):
    """Write the application's journey so far as events, oldest first."""
    from apps.admissions.models import Application, ApplicationEvent

    events = [ApplicationEvent(school=app.school, application=app, action="created", to_stage="enquiry", actor=office, note=app.get_source_display())]
    when = [at(app.enquired_on or app.created_at.date(), 10, 5)]
    order = Application.ORDER
    last = order.index(app.stage)
    span = max(1, ((app.stage_changed_at.date() if app.stage_changed_at else app.created_at.date()) - (app.enquired_on or app.created_at.date())).days)
    for i in range(1, last + 1):
        day = (app.enquired_on or app.created_at.date()) + timedelta(days=span * i // last)
        if i == last and app.stage_changed_at:
            moment = app.stage_changed_at
        else:
            moment = at(day, 11, 30)
        if order[i] == "documents" and app.assessment_score is not None:
            events.append(ApplicationEvent(school=app.school, application=app, action="scored", actor=office, note=f"{app.assessment_score}/{app.assessment_out_of}"))
            when.append(moment - timedelta(minutes=5))
        if order[i] == "offer":
            events.append(ApplicationEvent(school=app.school, application=app, action="approved", from_stage="documents", to_stage="offer", note="Approved by the principal"))
            when.append(moment)
            continue
        action = "admitted" if order[i] == "admitted" else "moved"
        events.append(ApplicationEvent(school=app.school, application=app, action=action, from_stage=order[i - 1], to_stage=order[i], actor=office))
        when.append(moment)
    if app.stage == "assessment" and app.assessment_at and app.assessment_score is None:
        events.append(ApplicationEvent(school=app.school, application=app, action="assessment", actor=office, note=app.assessment_at.isoformat()))
        when.append(app.stage_changed_at + timedelta(minutes=10))
    if app.stage == "documents" and app.documents_verified:
        events.append(ApplicationEvent(school=app.school, application=app, action="verified", actor=office))
        when.append(app.stage_changed_at + timedelta(hours=2))
    if app.closed:
        events.append(ApplicationEvent(school=app.school, application=app, action="closed", actor=office, note=app.closed_reason))
        when.append(app.stage_changed_at + timedelta(days=3))
    created = ApplicationEvent.objects.bulk_create(events)
    for event, moment in zip(created, when):
        event.created_at = moment
    ApplicationEvent.objects.bulk_update(created, ["created_at"])
    Application.objects.filter(pk=app.pk).update(created_at=when[0])


def _drop_students(ids):
    """Remove Student rows the console's Admit action created (and anything later seeds hung on them)."""
    from apps.fees.models import FeeInvoice, Payment, Refund

    if not ids:
        return
    Refund.objects.filter(payment__invoice__student_id__in=ids).delete()
    Payment.objects.filter(invoice__student_id__in=ids).delete()
    FeeInvoice.objects.filter(student_id__in=ids).delete()
    Student.objects.filter(id__in=ids).delete()
