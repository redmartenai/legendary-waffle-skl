"""
Seed "Sunrise Public School" with the sample data used in the EduFlow UI designs
(eduflow-ui/screens/*.dc.html), so every screen can be compared 1:1 with its mockup.

    python manage.py seed_design            # create (skips if already there)
    python manage.py seed_design --reset    # wipe and recreate

School code SUNRISE. The design's "today" (Tuesday 22 Sep) maps to the real school today, so the
data always looks current. Sign-in phones are printed at the end; OTP_DEV_ECHO shows the code.

Later phases extend this command as new modules (leave, approvals, admissions …) land.
"""

import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.academics.models import (
    AcademicYear,
    ClassGroup,
    Remark,
    Student,
    StudentGuardian,
    Subject,
    TeachingAssignment,
    TimetableSlot,
)
from apps.accounts.models import Department, Membership, Role, User
from apps.announcements.models import Announcement
from apps.attendance.models import AttendanceException, AttendanceSession
from apps.core import seed_console
from apps.core.tenant import unscoped, use_school
from apps.core.utils import school_today, school_tz
from apps.fees.models import FeeInvoice, Payment
from apps.fees.services import next_receipt_no
from apps.homework.models import Homework, HomeworkSubmission
from apps.messaging.models import Conversation, ConversationMember, Message
from apps.notifications.models import Notification
from apps.results.models import Exam, ExamMark
from apps.results.services import grade_for
from apps.tenancy.models import Organization, School
from apps.transport.models import Route, Stop, StudentTransport, Vehicle
from apps.transport.services import ensure_trips_for_date, refresh_route_length

CODE = "SUNRISE"
PHONE_PREFIX = "+91984"  # every phone this command creates starts with this

# Grade → sections. 15 grades, 38 sections (the design's "Nursery–12 · 38 sections").
GRADES = [
    ("Nursery", "AB"), ("LKG", "AB"), ("UKG", "AB"),
    ("1", "ABC"), ("2", "ABC"), ("3", "ABC"), ("4", "ABC"), ("5", "ABC"),
    ("6", "ABC"), ("7", "ABC"), ("8", "ABC"),
    ("9", "AB"), ("10", "AB"), ("11", "AB"), ("12", "AB"),
]

SUBJECTS = [
    # name, code, colour (the design's chart series c1–c4 plus pastel inks)
    ("Mathematics", "MATH", "#3446C8"),
    ("English", "ENG", "#D9622B"),
    ("Science", "SCI", "#1F8A70"),
    ("Hindi", "HIN", "#9B4DCA"),
    ("Social Studies", "SST", "#A24F1C"),
    ("Computer Science", "CS", "#2F62C4"),
    ("Art", "ART", "#A8354F"),
    ("Physical Education", "PE", "#1F6F4E"),
    ("Music", "MUS", "#7F6210"),
    ("Economics", "ECO", "#5A3FB0"),
]

# Named staff from the designs: name, role, title, subject code(s), department
NAMED_STAFF = [
    ("Priya Menon", Role.TEACHER, "Mathematics · Class teacher 6-B", ["MATH"], ""),
    ("Joseph Thomas", Role.TEACHER, "Science", ["SCI"], ""),
    ("Meera Krishnan", Role.TEACHER, "Science · HOD", ["SCI"], ""),
    ("Kavya Nair", Role.TEACHER, "English", ["ENG"], ""),
    ("Vikram Singh", Role.TEACHER, "Physical Education", ["PE"], ""),
    ("Sunita Verma", Role.TEACHER, "Hindi", ["HIN"], ""),
    ("Deepa Iyer", Role.TEACHER, "Primary · Class teacher 2-A", ["ENG", "MATH"], ""),
    ("Arjun Das", Role.TEACHER, "English", ["ENG"], ""),
    ("Nikhil Rao", Role.TEACHER, "Computer Science", ["CS"], ""),
    ("Farah Khan", Role.TEACHER, "Art", ["ART", "MUS"], ""),
    ("Rekha Kulkarni", Role.TEACHER, "Class teacher 6-A", ["SST"], ""),
    ("Sameer Naik", Role.TEACHER, "Class teacher 6-C", ["HIN"], ""),
    ("Meera Joshi", Role.TEACHER, "Economics", ["ECO", "SST"], ""),
    ("Ravi Kumar", Role.TEACHER, "Social Studies", ["SST"], ""),
]

FIRST = [
    "Aadhya", "Aanya", "Abhinav", "Aditi", "Aditya", "Advait", "Akshay", "Ananya", "Anika", "Arjun", "Arnav",
    "Avni", "Ayaan", "Charvi", "Dev", "Dhruv", "Diya", "Ira", "Ishaan", "Ishita", "Kabir", "Kiara", "Krish",
    "Meera", "Mihir", "Myra", "Navya", "Neel", "Nisha", "Pari", "Pranav", "Reyansh", "Riya", "Rohan", "Rudra",
    "Saanvi", "Sara", "Shaurya", "Tara", "Vihaan", "Vivaan", "Yash", "Zara", "Aisha", "Kunal", "Mahira",
]
LAST = [
    "Sharma", "Iyer", "Menon", "Reddy", "Nair", "Pillai", "Joshi", "Rao", "Khan", "Das", "Gupta", "Mehta",
    "Kapoor", "Bose", "Patel", "Verma", "Desai", "Kulkarni", "Shetty", "Bansal", "Chopra", "Arora", "Sen",
    "Batra", "Siddiqui", "Qureshi", "Nanda", "Malhotra", "Bhatt", "Dutta",
]

# 6-B roll call from StaffAttendance (rolls 1–14), Aarav is roll 14.
ROSTER_6B = [
    "Ananya Iyer", "Kabir Khan", "Meera Pillai", "Rohan Iyer", "Saanvi Reddy", "Vihaan Joshi", "Ishaan Verma",
    "Zara Sheikh", "Aditya Nair", "Tara Menon", "Arjun Bose", "Nisha Patel", "Dev Malhotra", "Aarav Sharma",
]

# 7-C mark sheet from StaffMarks (rolls 1–12).
ROSTER_7C = [
    "Aditi Rao", "Arnav Bhatt", "Bhavya Shetty", "Farhan Qureshi", "Gauri Kulkarni", "Ishita Dutta", "Karan Anand",
    "Lavanya Pillai", "Mihir Desai", "Nandini Hegde", "Pranav Shah", "Rohan Gupta",
]

# Priya Menon's Maths periods outside 6-B (weekday -> periods), so her week is 28 periods with no clashes.
# Tuesday matches StaffTimetable: P1 6-B, P2 7-A, P3 free, P4 6-A, P5 7-C, then a cover and a free period.
PRIYA_WEEK = {
    "6-A": {0: [4], 1: [4], 2: [1, 6], 3: [5], 4: [2], 5: [3]},
    "7-A": {0: [2, 7], 1: [2], 2: [5], 3: [4], 4: [3, 7], 5: [2]},
    "7-C": {0: [5], 1: [5], 2: [3, 7], 3: [1, 7], 4: [6]},
}

# School day (PTimetable): assembly 8:00, P1–P7, break after P3, lunch after P5, dismissal 2:30.
PERIODS = [
    (time(8, 20), time(9, 5)),
    (time(9, 5), time(9, 50)),
    (time(9, 50), time(10, 35)),
    (time(10, 50), time(11, 35)),
    (time(11, 35), time(12, 20)),
    (time(13, 0), time(13, 45)),
    (time(13, 45), time(14, 30)),
]

# 6-B week (Mon–Sat). Tuesday is the design's ribbon: Ma En Sc | Hi SS | CS Art.
WEEK_6B = [
    ["MATH", "ENG", "SCI", "HIN", "SST", "CS", "PE"],
    ["MATH", "ENG", "SCI", "HIN", "SST", "CS", "ART"],
    ["ENG", "MATH", "SST", "SCI", "HIN", "MUS", "PE"],
    ["SCI", "MATH", "ENG", "SST", "HIN", "CS", "ART"],
    ["MATH", "SCI", "ENG", "HIN", "SST", "PE", "ART"],
    ["MATH", "SCI", "ENG", "ART"],  # Saturday half day (remedial slot = Maths)
]

# Route 07 (Bengaluru north). Stops in morning pickup order; the school is last.
ROUTE_07_STOPS = [
    # name, lat, lng, pickup offset, drop offset
    ("Hebbal Depot", 13.0452, 77.5921, 0, 58),
    ("Kodigehalli Gate", 13.0561, 77.5801, 6, 52),
    ("Sahakar Nagar", 13.0621, 77.5870, 11, 46),
    ("Maple Residency Gate", 13.0579, 77.5995, 18, 27),
    ("Palm Grove", 13.0512, 77.6040, 24, 19),
    ("Lakeview Circle", 13.0455, 77.6085, 30, 11),
    ("ORR Junction", 13.0401, 77.6062, 35, 5),
    ("Nagawara", 13.0389, 77.6120, 38, 2),
    ("Sunrise Public School", 13.0358, 77.5970, 45, 0),
]


class Command(BaseCommand):
    help = "Seed Sunrise Public School with the EduFlow design sample data (school code SUNRISE)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete and recreate the design school")
        parser.add_argument("--only", help="Re-run one console seed module (apps/core/seed_console) on the existing school")

    def handle(self, *args, **options):
        random.seed(22092026)
        if options["only"]:
            # Outside unscoped(): that bypass would beat use_school and let the module touch other schools.
            with unscoped():
                existing = School.objects.filter(code=CODE).first()
            if not existing:
                self.stdout.write("No design school yet. Run seed_design first.")
                return
            with transaction.atomic(), use_school(existing):
                self._load(existing)
                seed_console.run(self, existing, only=options["only"])
            self.stdout.write(f"Re-ran console seed '{options['only']}'.")
            return
        with unscoped():
            existing = School.objects.filter(code=CODE).first()
            if existing and not options["reset"]:
                self.stdout.write("Design data already exists. Use --reset to recreate it.")
                return
            if existing:
                self._reset(existing)
            with transaction.atomic():
                org, _ = Organization.objects.get_or_create(slug="sunrise-trust", defaults={"name": "Sunrise Education Trust"})
                school = School.objects.create(
                    organization=org,
                    code=CODE,
                    name="Sunrise Public School",
                    short_name="Sunrise",
                    kind="k12",
                    city="Bengaluru",
                    state="Karnataka",
                    primary_color="#3446C8",
                    accent_color="#C9571F",
                    languages=["en", "hi"],
                    settings={
                        "campus": "Main Campus",
                        "address": "14 Lake View Road, Whitefield, Bengaluru 560066",
                        "grading": "letter_plus",
                        "chat": {"student_chat": True, "parent_to_subject_teachers": True},
                        "contacts": {"office": "+918041230000", "transport": "+918041230007"},
                        "payments": {"gateway": "mock"},
                        "terms": [
                            {"name": "Term 1", "starts_on": "2026-04-01", "ends_on": "2026-09-30"},
                            {"name": "Term 2", "starts_on": "2026-10-01", "ends_on": "2027-03-31"},
                        ],
                    },
                )
            with transaction.atomic(), use_school(school):
                self._seed(school)
        self._print_summary()

    # ------------------------------------------------------------------ helpers

    def _load(self, school):
        """Rebuild what ``_seed`` keeps on ``self`` from the database (for ``--only``)."""
        self.today = school_today(school)
        self.tz = school_tz(school)
        self.groups = {g.short_label: g for g in ClassGroup.objects.all()}
        self.students = {(s.class_group.short_label, s.full_name): s for s in Student.objects.select_related("class_group")}
        self.subjects = {s.code: s for s in Subject.objects.all()}
        self.principal = User.objects.get(phone=self._phone(1))
        self.parent = User.objects.get(phone="+919845034521")
        self.staff = {u.full_name: u for u in User.objects.filter(memberships__school=school, memberships__role__in=[Role.TEACHER, Role.ADMIN, Role.ACCOUNTANT, Role.TRANSPORT_MANAGER]).distinct()}

    def _reset(self, school):
        Notification.objects.filter(school=school).delete()
        org_id = school.organization_id
        with use_school(school):
            # Refunds protect payments, payments protect their invoices, and RESTRICT on Student.class_group
            # means students go before their classes, so clear them in that order.
            from apps.fees.models import Refund

            Refund.objects.all().delete()
            Payment.objects.all().delete()
            Student.objects.all().delete()
        school.delete()
        if not School.objects.filter(organization_id=org_id).exists():
            Organization.objects.filter(id=org_id).delete()
        User.objects.filter(phone__startswith=PHONE_PREFIX).delete()
        self.stdout.write("Removed previous design data.")

    def _user(self, phone, name, school, role, email="", **membership):
        user = User.objects.filter(phone=phone).first() or User.objects.create_user(phone, name, email=email)
        Membership.objects.get_or_create(user=user, role=role, school=school, defaults=membership)
        return user

    def _phone(self, n):
        return f"{PHONE_PREFIX}{n:07d}"

    # ------------------------------------------------------------------ main

    def _seed(self, school):
        today = school_today(school)
        tz = school_tz(school)
        self.today = today
        # The PTM is the first Saturday at least a week away (the designs' "Sat, 3 Oct").
        ptm = today + timedelta(days=7)
        self.ptm = ptm + timedelta(days=(5 - ptm.weekday()) % 7)
        self.ptm_short = f"{self.ptm:%a}, {self.ptm.day} {self.ptm:%b}"
        self.ptm_long = f"{self.ptm.day} {self.ptm:%B}"
        year = AcademicYear.objects.create(name="2026–27", starts_on=date(2026, 4, 1), ends_on=date(2027, 3, 31), is_current=True)
        subjects = {code: Subject.objects.create(name=name, code=code, color=color) for name, code, color in SUBJECTS}

        # --- people -------------------------------------------------------------
        principal = self._user(
            self._phone(1), "Dr. Anita Rao", school, Role.PRINCIPAL, email="anita.rao@sunrise.test", title="Principal"
        )
        principal.set_password("sunrise-principal")
        principal.save(update_fields=["password"])
        staff = {}
        for i, (name, role, title, _codes, dept) in enumerate(NAMED_STAFF, start=10):
            staff[name] = self._user(self._phone(i), name, school, role, title=title, department=dept)
        staff["Priya Menon"].memberships.filter(school=school).update(
            settings={"office_hours": {"days": [0, 1, 2, 3, 4], "start": "15:00", "end": "17:00"}, "employee_id": "SPS-T-0142", "joined_on": "2018-06-01"}
        )
        # Generated teachers bring the teaching staff to 86.
        generated = []
        n = 100
        while len(staff) + len(generated) < 86:
            name = f"{random.choice(FIRST)} {random.choice(LAST)}"
            generated.append(self._user(self._phone(n), name, school, Role.TEACHER, title="Teacher"))
            n += 1
        accountant = self._user(self._phone(40), "Deepak Nair", school, Role.ACCOUNTANT, title="Accounts", department=Department.ACCOUNTS)
        transport_mgr = self._user(self._phone(41), "Ramesh Gowda", school, Role.TRANSPORT_MANAGER, title="Transport desk", department=Department.TRANSPORT)
        office = self._user(self._phone(42), "Shalini Rao", school, Role.ADMIN, title="School office", department=Department.OFFICE)
        admissions = self._user(self._phone(43), "Nandini Shetty", school, Role.ADMIN, title="Admissions", department=Department.ADMISSIONS)
        driver = self._user(self._phone(44), "Suresh Kumar", school, Role.DRIVER, title="Driver · Route 07")
        attendant = self._user(self._phone(45), "Lakshmi R.", school, Role.ATTENDANT, title="Attendant · Route 07")
        # 32 support staff in total (the office, transport and crew above count towards it).
        for i in range(26):
            self._user(self._phone(200 + i), f"{random.choice(FIRST)} {random.choice(LAST)}", school, Role.ADMIN, title="Support staff", department=Department.OFFICE)

        # --- classes ------------------------------------------------------------
        teachers_pool = list(staff.values()) + generated
        class_teachers = {"6-A": staff["Rekha Kulkarni"], "6-B": staff["Priya Menon"], "6-C": staff["Sameer Naik"], "2-A": staff["Deepa Iyer"]}
        groups = {}
        pool_iter = iter([t for t in generated])
        for grade, sections in GRADES:
            for section in sections:
                key = f"{grade}-{section}"
                teacher = class_teachers.get(key) or next(pool_iter, random.choice(generated))
                groups[key] = ClassGroup.objects.create(academic_year=year, grade=grade, section=section, class_teacher=teacher)
        self.groups = groups

        # --- students -----------------------------------------------------------
        parent = self._user("+919845034521", "Rahul Sharma", school, Role.PARENT, title="Parent")
        self.parent = parent
        students = {}
        total = 0
        sizes = self._section_sizes(groups)
        guardian_n = 1000
        for key, group in groups.items():
            names = self._names_for(key, sizes[key])
            for roll, name in enumerate(names, start=1):
                grade = group.grade
                adm_year = 2019 if key == "6-B" else 2026 - (int(grade) if grade.isdigit() else 0) + random.randint(-1, 0)
                student = Student(
                    school=school,
                    full_name=name,
                    admission_no=f"SPS/{adm_year}/{total + 1:04d}",
                    roll_no=roll,
                    class_group=group,
                    gender=random.choice(["male", "female"]),
                    date_of_birth=date(2026 - 6 - (int(grade) if grade.isdigit() else -2), random.randint(1, 12), random.randint(1, 28)),
                )
                if name == "Aarav Sharma" and key == "6-B":
                    student.admission_no, student.date_of_birth, student.gender = "SPS/2019/0457", date(2015, 3, 14), "male"
                if name == "Diya Sharma" and key == "2-A":
                    student.admission_no, student.date_of_birth, student.gender = "SPS/2023/1182", date(2019, 7, 2), "female"
                student.save()
                students[(key, name)] = student
                total += 1
        aarav = students[("6-B", "Aarav Sharma")]
        diya = students[("2-A", "Diya Sharma")]
        StudentGuardian.objects.create(student=aarav, user=parent, relationship="father", is_primary=True)
        StudentGuardian.objects.create(student=diya, user=parent, relationship="father", is_primary=True)
        aarav_user = self._user(self._phone(3), "Aarav Sharma", school, Role.STUDENT, title="Student · 6-B")
        aarav.user = aarav_user
        aarav.save(update_fields=["user"])
        # Guardians for the rest of 6-B (so teacher screens have parents to message).
        for (key, name), student in students.items():
            if key == "6-B" and name != "Aarav Sharma":
                g = self._user(self._phone(guardian_n), f"{random.choice(FIRST)} {name.split()[-1]}", school, Role.PARENT, title="Parent")
                StudentGuardian.objects.create(student=student, user=g, relationship="parent", is_primary=True)
                guardian_n += 1
        self.students = students

        # --- teaching & timetable -----------------------------------------------
        self._seed_teaching(groups, subjects, staff, generated)

        # --- modules --------------------------------------------------------------
        self._seed_transport(school, aarav, driver, attendant, today)
        self._seed_attendance(school, groups, aarav, diya, today, tz)
        self._seed_results(groups, subjects, aarav, diya)
        self._seed_fees(school, groups, aarav, diya, parent, today, tz)
        self._seed_homework(groups, subjects, staff, aarav, parent, today)
        self._seed_remarks(aarav, staff, today)
        self._seed_announcements(principal, today)
        self._seed_chat(school, parent, aarav, diya, staff, office, transport_mgr, today)
        self._seed_notifications(school, parent, today)
        self._seed_parent_extras(school, groups, subjects, staff, principal, parent, aarav, diya, today, tz)
        self._seed_student_extras(school, groups, subjects, staff, aarav, today, tz)
        self._seed_staff_extras(school, groups, subjects, staff, principal, aarav, today, tz)
        self._seed_principal_extras(school, groups, subjects, staff, principal, today, tz)
        self.staff, self.subjects, self.principal, self.tz = staff, subjects, principal, tz
        seed_console.run(self, school)
        _ = (accountant, admissions)
        self.stdout.write(f"Created {total} students in {len(groups)} sections.")

    # ------------------------------------------------------------------ pieces

    def _section_sizes(self, groups):
        """Spread 1,248 students over the sections; 6-B has 38 and 2-A has 34."""
        fixed = {"6-B": 38, "6-A": 36, "7-A": 40, "7-C": 34, "2-A": 34}
        rest = [k for k in groups if k not in fixed]
        remaining = 1248 - sum(fixed.values())
        base = remaining // len(rest)
        sizes = {k: base for k in rest}
        for k in rest[: remaining - base * len(rest)]:
            sizes[k] += 1
        sizes.update(fixed)
        return sizes

    def _names_for(self, key, size):
        taken = set()
        names = []
        if key == "6-B":
            names = list(ROSTER_6B)
            taken = set(names)
        if key == "7-C":
            names = list(ROSTER_7C)
            taken = set(names)
        while len(names) < size:
            name = f"{random.choice(FIRST)} {random.choice(LAST)}"
            if name in taken or name in ("Aarav Sharma", "Diya Sharma"):
                continue
            taken.add(name)
            names.append(name)
        if key == "2-A":
            names[7] = "Diya Sharma"  # roll 08
        if key == "6-B":
            # Rolls 1–14 are the design's roll call; the rest are sorted after them.
            names = names[:14] + sorted(names[14:])
        if key == "7-C":
            names = names[:12] + sorted(names[12:])
        return names

    def _seed_teaching(self, groups, subjects, staff, generated):
        g = random.Random(7)
        by_subject = {code: [] for code in subjects}
        for name, _role, _title, codes, _dept in NAMED_STAFF:
            for code in codes:
                by_subject[code].append(staff[name])
        for t in generated:
            by_subject[g.choice(list(subjects))].append(t)
        # Priya teaches exactly the four sections in the designs.
        by_subject["MATH"] = [t for t in by_subject["MATH"] if t != staff["Priya Menon"]]

        fixed = {
            "6-B": {"MATH": "Priya Menon", "SCI": "Joseph Thomas", "ENG": "Kavya Nair", "HIN": "Sunita Verma", "SST": "Ravi Kumar", "CS": "Nikhil Rao", "ART": "Farah Khan", "PE": "Vikram Singh", "MUS": "Farah Khan"},
            "6-A": {"MATH": "Priya Menon"},
            "7-A": {"MATH": "Priya Menon"},
            "7-C": {"MATH": "Priya Menon"},
            "6-C": {"ENG": "Kavya Nair"},
            "8-A": {"SCI": "Joseph Thomas"},
            "2-A": {"ENG": "Deepa Iyer", "MATH": "Deepa Iyer"},
        }
        core = ["MATH", "ENG", "SCI", "HIN", "SST", "CS", "ART", "PE"]
        self.assignments = {}
        for key, group in groups.items():
            chosen = {}
            for code in core + (["ECO"] if group.grade in ("11", "12") else []):
                name = fixed.get(key, {}).get(code)
                chosen[code] = staff[name] if name else g.choice(by_subject[code] or generated)
                TeachingAssignment.objects.create(teacher=chosen[code], class_group=group, subject=subjects[code])
            if key == "6-B":
                TeachingAssignment.objects.create(teacher=chosen["ART"], class_group=group, subject=subjects["MUS"])
                chosen["MUS"] = chosen["ART"]
            self.assignments[key] = chosen
            if key == "6-B":
                week = WEEK_6B
            elif key in PRIYA_WEEK:
                others = [c for c in core if c != "MATH"]
                week = []
                for weekday in range(6):
                    day = g.sample(others, 7 if weekday < 5 else 4)
                    for period in PRIYA_WEEK[key].get(weekday, []):
                        day[period - 1] = "MATH"
                    week.append(day)
            else:
                week = [g.sample([c for c in core if c != "MATH"], 6) + ["MATH"] for _ in range(5)] + [g.sample(core, 4)]
                week = [g.sample(day, len(day)) for day in week]
            room = {"6-B": "Room 204", "6-A": "Room 202", "7-A": "Room 305", "7-C": "Room 308", "6-C": "Room 207"}.get(key, f"Room {100 + list(groups).index(key)}")
            for weekday, day in enumerate(week):
                for period, code in enumerate(day, start=1):
                    start, end = PERIODS[period - 1]
                    teacher = chosen.get(code)
                    slot_room = {"SCI": "Lab 1", "CS": "Lab 2", "ART": "Art Room", "PE": "Ground", "MUS": "Music Room"}.get(code, room)
                    TimetableSlot.objects.create(
                        class_group=group, weekday=weekday, period=period, starts_at=start, ends_at=end,
                        subject=subjects[code], teacher=teacher, room=slot_room,
                    )

    def _seed_transport(self, school, aarav, driver, attendant, today):
        vehicle = Vehicle.objects.create(
            registration_no="KA 05 MN 2231", label="Bus 07", capacity=40, gps_device_id="358899052231070",
            has_gps_tracker=True, has_panic_button=True, has_cctv=True, has_speed_governor=True,
            fitness_valid_until=date(2027, 3, 31), permit_valid_until=date(2027, 1, 31),
            insurance_valid_until=date(2027, 2, 28), puc_valid_until=date(2026, 12, 31),
        )
        path = [[lat, lng] for _n, lat, lng, _p, _d in ROUTE_07_STOPS]
        route = Route.objects.create(
            code="07", name="Route 07", path=path, avg_speed_kmh=20, vehicle=vehicle, driver=driver, attendant=attendant,
            pickup_start=time(7, 1), drop_start=time(14, 40),
        )
        refresh_route_length(route)
        stops = {}
        for seq, (name, lat, lng, pickup, drop) in enumerate(ROUTE_07_STOPS, start=1):
            stops[name] = Stop.objects.create(
                route=route, name=name, lat=lat, lng=lng, sequence=seq, pickup_offset_min=pickup,
                drop_offset_min=drop, is_school=name == "Sunrise Public School",
            )
        StudentTransport.objects.create(student=aarav, route=route, pickup_stop=stops["Maple Residency Gate"], drop_stop=stops["Maple Residency Gate"])
        # 33 more riders so "on board 32 of 34" works.
        riders = [s for (k, _n), s in self.students.items() if k.startswith(("5-", "6-", "7-", "8-")) and s.id != aarav.id]
        stop_names = [n for n, *_ in ROUTE_07_STOPS[1:-1]]
        for s in random.sample(riders, 33):
            stop = stops[random.choice(stop_names)]
            StudentTransport.objects.create(student=s, route=route, pickup_stop=stop, drop_stop=stop)
        ensure_trips_for_date(route, today)

    def _school_day(self, today, offset):
        """`offset` school days from today (Mon–Sat), skipping Sundays either way."""
        day, step, left = today, (1 if offset >= 0 else -1), abs(offset)
        while left:
            day += timedelta(days=step)
            if day.weekday() != 6:
                left -= 1
        return day

    def _school_days(self, today, count):
        """The last `count` school days before today (Mon–Sat), oldest first."""
        days, d = [], today - timedelta(days=1)
        while len(days) < count:
            if d.weekday() != 6:
                days.append(d)
            d -= timedelta(days=1)
        return list(reversed(days))

    def _seed_attendance(self, school, groups, aarav, diya, today, tz):
        days = self._school_days(today, 76)  # the design's "71 of 76 this year"
        # Aarav: 5 absences in the year; this month one absence (~9 school days ago) and one late.
        aarav_marks = {days[-11]: ("absent", "Fever"), days[-7]: ("late", "In at 8:07 AM")}
        for i in (5, 19, 33, 52):
            aarav_marks[days[i]] = ("absent", "")
        weak = {"9-A": 0.11, "7-C": 0.09}
        exceptions = []
        for key, group in groups.items():
            members = list(Student.objects.filter(class_group=group).values_list("id", flat=True))
            rate = weak.get(key, 0.045)
            for day in days:
                session = AttendanceSession.objects.create(
                    class_group=group, date=day, marked_by=group.class_teacher,
                    marked_at=datetime.combine(day, time(8, 24), tzinfo=tz),
                )
                for sid in members:
                    if sid == aarav.id:
                        mark = aarav_marks.get(day)
                        if mark:
                            exceptions.append(AttendanceException(school=school, session=session, student_id=sid, status=mark[0], note=mark[1]))
                        continue
                    if sid == diya.id:
                        continue
                    r = random.random()
                    if r < rate:
                        exceptions.append(AttendanceException(school=school, session=session, student_id=sid, status="absent"))
                    elif r < rate + 0.012:
                        exceptions.append(AttendanceException(school=school, session=session, student_id=sid, status="late"))
        # Today: every register marked by 9:05; exactly 58 absent and 14 late school-wide
        # (6-B: Kabir absent, Zara late; 9-A and 7-C are the weak sections).
        absent_quota = {"6-B": 1, "9-A": 6, "7-C": 4}
        late_quota = {"6-B": 1}
        keys = list(groups)
        spare = [k for k in keys if k not in absent_quota]
        for k in random.choices(spare, k=58 - sum(absent_quota.values())):
            absent_quota[k] = absent_quota.get(k, 0) + 1
        for k in random.choices(spare, k=14 - sum(late_quota.values())):
            late_quota[k] = late_quota.get(k, 0) + 1
        kabir = self.students[("6-B", "Kabir Khan")].id
        zara = self.students[("6-B", "Zara Sheikh")].id
        for key, group in groups.items():
            session = AttendanceSession.objects.create(
                class_group=group, date=today, marked_by=group.class_teacher,
                marked_at=datetime.combine(today, time(8, 24 if key == "6-B" else random.randint(25, 59)), tzinfo=tz),
            )
            members = [
                sid for sid in Student.objects.filter(class_group=group).values_list("id", flat=True)
                if sid not in (aarav.id, diya.id, kabir, zara)
            ]
            random.shuffle(members)
            absent = [kabir] if key == "6-B" else []
            late = [zara] if key == "6-B" else []
            absent += members[: absent_quota.get(key, 0) - len(absent)]
            late += members[len(absent) : len(absent) + late_quota.get(key, 0) - len(late)]
            for sid in absent:
                exceptions.append(AttendanceException(school=school, session=session, student_id=sid, status="absent", note="Fever" if sid == kabir else ""))
            for sid in late:
                exceptions.append(AttendanceException(school=school, session=session, student_id=sid, status="late", note="In at 8:12 AM"))
        AttendanceException.objects.bulk_create(exceptions, batch_size=2000)

    def _seed_results(self, groups, subjects, aarav, diya):
        codes = ["ENG", "HIN", "MATH", "SCI", "SST", "CS"]
        aarav_scores = {
            "Unit Test 1": {"CS": 90, "MATH": 88, "ENG": 85, "SCI": 82, "HIN": 76, "SST": 70},
            "Unit Test 2": {"CS": 94, "MATH": 92, "ENG": 88, "SCI": 85, "HIN": 79, "SST": 76},
        }
        marks = []
        for key, group in groups.items():
            if not group.grade.isdigit():
                continue
            ids = list(Student.objects.filter(class_group=group).values_list("id", flat=True))
            for name, held_on, bump in (("Unit Test 1", self.today - timedelta(days=64), 0), ("Unit Test 2", self.today - timedelta(days=4), 2)):
                exam = Exam.objects.create(class_group=group, name=name, held_on=held_on, is_published=True, published_on=min(self.today, held_on + timedelta(days=3)))
                grade_bias = -6 if group.grade == "9" and name == "Unit Test 2" else 0
                for sid in ids:
                    base = random.gauss(76 + bump + grade_bias, 9)
                    for code in codes:
                        if sid == aarav.id:
                            pct = aarav_scores[name][code]
                        else:
                            pct = max(28, min(99, round(base + random.gauss(0, 6))))
                        marks.append(ExamMark(school=group.school, exam=exam, student_id=sid, subject=subjects[code], marks=Decimal(pct), max_marks=Decimal(100)))
        ExamMark.objects.bulk_create(marks, batch_size=5000)

    def _seed_fees(self, school, groups, aarav, diya, parent, today, tz):
        """Term 1 mostly paid (₹1.86 Cr of ₹2.40 Cr is the design's school view); Term 2 due in 8 days."""
        due_t2 = self._school_day(today, 7)
        self.due_t2 = due_t2
        aarav_plan = [
            ("Term 1 tuition", "tuition", 42500, date(2026, 4, 30), True, date(2026, 4, 22)),
            ("Activity fee 2026–27", "activity", 10000, date(2026, 6, 30), True, date(2026, 6, 18)),
            ("Transport · Q1", "transport", 6000, date(2026, 4, 30), True, date(2026, 4, 22)),
            ("Transport · Q2", "transport", 6000, date(2026, 7, 1), True, date(2026, 7, 1)),
            ("Term 2 tuition", "tuition", 42500, due_t2, False, None),
            ("Transport · Q3", "transport", 6000, due_t2 + timedelta(days=1), False, None),
            ("Transport · Q4", "transport", 6000, date(2027, 1, 1), False, None),
        ]
        for title, cat, amount, due, paid, paid_on in aarav_plan:
            self._invoice(school, aarav, title, cat, amount, due, paid, paid_on, parent, tz)
        self._invoice(school, diya, "Term 1 tuition", "tuition", 38000, date(2026, 4, 30), True, date(2026, 4, 22), parent, tz)
        self._invoice(school, diya, "Term 2 tuition", "tuition", 38000, due_t2, False, None, parent, tz)
        # Everyone else: Term 1 invoice, paid for most; ~142 families overdue.
        others = Student.objects.exclude(id__in=[aarav.id, diya.id]).select_related("class_group")
        invoices = []
        for s in others:
            amount = 30000 if not s.class_group.grade.isdigit() else 32000 + int(s.class_group.grade) * 900
            overdue = random.random() < 0.115
            invoices.append(FeeInvoice(
                school=school, student=s, title="Term 1 tuition", category="tuition", amount=Decimal(amount),
                due_date=date(2026, 4, 30), paid_amount=Decimal(0) if overdue else Decimal(amount),
            ))
        FeeInvoice.objects.bulk_create(invoices, batch_size=2000)

    def _invoice(self, school, student, title, cat, amount, due, paid, paid_on, payer, tz):
        inv = FeeInvoice.objects.create(
            student=student, title=title, category=cat, amount=Decimal(amount), due_date=due,
            paid_amount=Decimal(amount) if paid else Decimal(0),
        )
        if paid:
            Payment.objects.create(
                invoice=inv, amount=inv.amount, gateway="mock", status=Payment.Status.SUCCEEDED,
                gateway_order_id="seed", gateway_payment_id="seed", paid_by=payer,
                paid_at=datetime.combine(paid_on, time(19, 12), tzinfo=tz), receipt_no=next_receipt_no(school, paid_on),
            )
        return inv

    def _seed_homework(self, groups, subjects, staff, aarav, parent, today):
        g6b = groups["6-B"]
        a = self.assignments["6-B"]
        items = [
            ("MATH", "Exercise 7.3, Q1–12 (Fractions)", "Solve Q1–12 in the Maths notebook. Show every step; simplify each answer.", -1, 1),
            ("ENG", "Diary entry · A day at the lake", "Write 150–200 words in diary format. Mind the date, greeting and signing off.", -1, 2),
            ("SCI", "Lab record · Separation of substances", "Complete the lab record for experiments 3 and 4 with labelled diagrams.", -6, 3),
            ("SST", "Map work · Rivers of India", "Mark the major rivers and their tributaries on the outline map.", 0, 6),
            ("HIN", "पत्र लेखन · Letter to a friend", "Write an informal letter in Hindi (120 words).", -12, -6),
            ("CS", "Scratch animation", "Build a 20-second animation using loops.", -20, -10),
        ]
        for code, title, desc, assigned, due in items:
            hw = Homework.objects.create(
                class_group=g6b, subject=subjects[code], title=title, description=desc, assigned_by=a[code],
                assigned_on=self._school_day(today, assigned), due_date=self._school_day(today, due), accepts_photos=True,
            )
            if due < 0:
                HomeworkSubmission.objects.create(
                    homework=hw, student=aarav, submitted_by=parent,
                    submitted_at=timezone.now() + timedelta(days=due - 1), status=HomeworkSubmission.Status.REVIEWED,
                    teacher_remark={"HIN": "Neat handwriting. Watch the matras. B+", "CS": "Lovely use of loops! 18/20"}[code],
                )
        # Ex 7.2 for 6-A: 31 submissions waiting for review (StaffHome "marking pile").
        hw = Homework.objects.create(
            class_group=groups["6-A"], subject=subjects["MATH"], title="Exercise 7.2", description="Q1–15, fractions on a number line.",
            assigned_by=staff["Priya Menon"], assigned_on=today - timedelta(days=4), due_date=today - timedelta(days=1), accepts_photos=True,
        )
        for s in Student.objects.filter(class_group=groups["6-A"])[:31]:
            HomeworkSubmission.objects.create(homework=hw, student=s, submitted_by=None, submitted_at=timezone.now() - timedelta(hours=random.randint(10, 40)))

    def _seed_remarks(self, aarav, staff, today):
        science = Homework.objects.filter(class_group=aarav.class_group, subject__code="SCI").first()
        items = [
            ("Priya Menon", "positive", "Aarav solved the fractions challenge ahead of the class. Encourage him to write out his steps — it will help in the half-yearly exam.", 3, None),
            ("Joseph Thomas", "concern", "Lab record for Experiment 4 is incomplete. Please ensure it is submitted by Friday.", 6, science),
            ("Kavya Nair", "info", "Reads confidently aloud; vocabulary journal is up to date.", 12, None),
        ]
        from apps.academics.models import RemarkAck

        for name, tone, body, days_ago, homework in items:
            r = Remark.objects.create(
                student=aarav, author=staff[name], body=body, tone=tone, homework=homework, requires_ack=tone == "concern"
            )
            Remark.objects.filter(pk=r.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
            # Rahul has already acknowledged the praise and the general note; the concern is waiting.
            if tone != "concern":
                ack = RemarkAck.objects.create(remark=r, user=self.parent)
                RemarkAck.objects.filter(pk=ack.pk).update(created_at=timezone.now() - timedelta(days=days_ago - 1))

    def _seed_announcements(self, principal, today):
        now = timezone.now()
        items = [
            (f"Parent–teacher meeting · {self.ptm_short}", "No regular classes. Book a 15-minute slot with the class teacher in the app.", Announcement.Kind.EVENT, Announcement.Audience.FAMILIES, True, 20),
            ("Half-yearly exam timetable — Grades 6–8", "The half-yearly exams start on Monday 12 October. The date sheet is attached.", Announcement.Kind.EXAM, Announcement.Audience.FAMILIES, True, 50),
            ("Route 07 running late today", "Traffic at the ORR junction. The drop run leaves at 2:52 PM.", Announcement.Kind.TRANSPORT, Announcement.Audience.FAMILIES, False, 0),
            ("Half-yearly invigilation roster", "Please confirm your duties by Thursday.", Announcement.Kind.EXAM, Announcement.Audience.STAFF, True, 3),
        ]
        tz = school_tz(principal.memberships.first().school)
        ptm_day = self.ptm
        events = {
            f"Parent–teacher meeting · {self.ptm_short}": (
                datetime.combine(ptm_day, time(9, 0), tzinfo=tz),
                datetime.combine(ptm_day, time(12, 30), tzinfo=tz),
                "Classrooms",
            ),
            "Half-yearly exam timetable — Grades 6–8": (
                datetime.combine(today + timedelta(days=20), time(9, 0), tzinfo=tz),
                datetime.combine(today + timedelta(days=31), time(11, 30), tzinfo=tz),
                "",
            ),
        }
        for title, body, kind, audience, ack, hours_ago in items:
            starts, ends, where = events.get(title, (None, None, ""))
            Announcement.objects.create(
                title=title, body=body, kind=kind, audience=audience, requires_ack=ack, created_by=principal,
                published_at=now - timedelta(hours=hours_ago), event_starts_at=starts, event_ends_at=ends, location=where,
            )

    def _seed_chat(self, school, parent, aarav, diya, staff, office, transport_mgr, today):
        now = timezone.now()
        priya = staff["Priya Menon"]
        direct = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=aarav)
        ConversationMember.objects.create(conversation=direct, user=parent, side="family", label="Parent of Aarav")
        ConversationMember.objects.create(conversation=direct, user=priya, side="staff", label="Class teacher · 6-B")
        self._messages(direct, [
            (parent, "Good morning Ms. Menon. Aarav found Exercise 7.3 tricky yesterday. Could you share some extra practice?", timedelta(hours=2, minutes=30)),
            (priya, "Of course! Here's a short practice sheet on fractions. Ten minutes a day is plenty.", timedelta(hours=1, minutes=50)),
            (parent, f"Thank you. Could we also meet at the PTM on {self.ptm_long}? Any time in the morning works.", timedelta(hours=1, minutes=10)),
            (priya, f"Sure — I've reserved 10:15 AM for you on {self.ptm.day} {self.ptm:%b}.", timedelta(minutes=18)),
        ], now)
        ConversationMember.objects.filter(conversation=direct, user=parent).update(last_read_at=now - timedelta(hours=1))
        ConversationMember.objects.filter(conversation=direct, user=priya).update(last_read_at=now)

        others = [
            (staff["Joseph Thomas"], "Science · 6-B", "Thanks — the lab record looks much better now.", timedelta(days=1)),
            (staff["Deepa Iyer"], "Class teacher · 2-A", "Diya read her story aloud beautifully today!", timedelta(days=4)),
        ]
        for teacher, label, body, ago in others:
            c = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=aarav if "6-B" in label else diya)
            ConversationMember.objects.create(conversation=c, user=parent, side="family", label="Parent")
            ConversationMember.objects.create(conversation=c, user=teacher, side="staff", label=label)
            self._messages(c, [(teacher, body, ago)], now)
            ConversationMember.objects.filter(conversation=c).update(last_read_at=now)
        for dept, member, body, ago in (
            (Department.OFFICE, office, "Your bonafide certificate is ready for collection.", timedelta(days=8)),
            (Department.TRANSPORT, transport_mgr, "Route 07 stop timings are updated from Monday.", timedelta(days=14)),
        ):
            c = Conversation.objects.create(kind=Conversation.Kind.DEPARTMENT, department=dept, student=aarav)
            ConversationMember.objects.create(conversation=c, user=parent, side="family", label="Parent of Aarav")
            ConversationMember.objects.create(conversation=c, user=member, side="staff", label=member.memberships.filter(school=school).first().title)
            self._messages(c, [(member, body, ago)], now)
            ConversationMember.objects.filter(conversation=c).update(last_read_at=now)

    def _seed_parent_extras(self, school, groups, subjects, staff, principal, parent, aarav, diya, today, tz):
        """Data the Parent app screens need beyond the basics (Phase 2)."""
        from django.core.files.base import ContentFile

        from apps.academics.models import TimetableSlot
        from apps.attendance.models import LeaveApplication
        from apps.documents.models import CertificateRequest, Document
        from apps.fees.models import FeeInvoiceItem
        from apps.homework.models import HomeworkAttachment
        from apps.messaging.models import Meeting
        from apps.results.models import ReportCardNote
        from apps.transport.models import BoardingEvent, Direction, Trip

        # Houses (the ID card and profile show them).
        houses = ["Teal", "Crimson", "Amber", "Indigo"]
        for i, student in enumerate(Student.objects.all().only("id")):
            Student.objects.filter(pk=student.pk).update(house=houses[i % 4])
        Student.objects.filter(pk=aarav.pk).update(house="Teal")
        Student.objects.filter(pk=diya.pk).update(house="Amber")

        # Fee heads: what the ₹42,500 covers.
        heads_t2 = [("Tuition", 33500), ("Science lab", 2500), ("Computer lab", 2000), ("Library", 1500), ("Examinations", 1500), ("Sports & activities", 1500)]
        heads_diya = [("Tuition", 31000), ("Library", 1500), ("Examinations", 1500), ("Sports & activities", 1500), ("Activity kit", 2500)]
        for invoice in FeeInvoice.objects.filter(student__in=[aarav, diya]):
            if invoice.title.startswith(("Term 1", "Term 2")):
                heads = heads_t2 if invoice.student_id == aarav.id else heads_diya
            elif invoice.category == "transport":
                heads = [("Bus · Route 07", int(invoice.amount))]
            else:
                heads = [("Clubs, trips & annual day", int(invoice.amount))]
            for order, (head, amount) in enumerate(heads):
                FeeInvoiceItem.objects.create(invoice=invoice, head=head, amount=Decimal(amount), order=order)
        for n, payment in enumerate(Payment.objects.filter(invoice__student__in=[aarav, diya])):
            Payment.objects.filter(pk=payment.pk).update(method=["upi", "card", "upi", "netbanking"][n % 4])

        # Homework: worksheets, grades and the checked copy.
        hw = {h.subject.code: h for h in Homework.objects.filter(class_group=groups["6-B"]).select_related("subject")}
        for code, name in (("MATH", "Ex_7.3.pdf"), ("ENG", "Diary_entry_format.pdf"), ("SCI", "Lab_record_template.pdf"), ("SST", "Rivers_outline_map.pdf")):
            pdf = _pdf(name.replace("_", " ").removesuffix(".pdf"), hw[code].description)
            a = HomeworkAttachment(homework=hw[code], name=name, size=len(pdf))
            a.file.save(name, ContentFile(pdf), save=False)
            a.save()
        for code, grade in (("HIN", "B+"), ("CS", "18/20")):
            sub = HomeworkSubmission.objects.get(homework=hw[code], student=aarav)
            sub.grade = grade
            if code == "HIN":
                pdf = _pdf("Checked copy", "पत्र लेखन — checked by Sunita Verma")
                sub.checked_copy.save("Hindi_letter_checked.pdf", ContentFile(pdf), save=False)
            sub.save()
        # "Bring your geometry box" on the next Maths P1.
        TimetableSlot.objects.filter(class_group=groups["6-B"], subject__code="MATH", period=1).update(note="Bring your geometry box")

        # Report card: class-teacher note, and the half-yearly exam still to come.
        ut2 = Exam.objects.get(class_group=groups["6-B"], name="Unit Test 2")
        ReportCardNote.objects.create(
            exam=ut2, student=aarav, author=staff["Priya Menon"],
            body="Up in every subject. Before the half-yearly, keep writing out each step in Maths.",
        )
        ReportCardNote.objects.filter(exam=ut2, student=aarav).update(created_at=timezone.now() - timedelta(days=1))
        for group in groups.values():
            if group.grade.isdigit():
                Exam.objects.create(class_group=group, name="Half-yearly", held_on=today + timedelta(days=20), is_published=False, results_on=today + timedelta(days=38))

        # PTM booked in the chat with Ms. Menon, plus her practice sheet.
        conversation = Conversation.objects.filter(kind=Conversation.Kind.DIRECT, student=aarav, members__user=staff["Priya Menon"]).first()
        ptm = self.ptm
        Meeting.objects.create(
            conversation=conversation, title="Parent–teacher meeting", location="Room 204",
            starts_at=datetime.combine(ptm, time(10, 15), tzinfo=tz), ends_at=datetime.combine(ptm, time(10, 30), tzinfo=tz),
            booked_by=staff["Priya Menon"],
        )
        practice = conversation.messages.filter(sender=staff["Priya Menon"]).order_by("created_at").first()
        pdf = _pdf("Fractions practice", "Ten short questions on equivalent and unlike fractions.")
        practice.attachment.save("Fractions_practice.pdf", ContentFile(pdf), save=False)
        practice.attachment_name = "Fractions_practice.pdf"
        practice.attachment_size = len(pdf)
        practice.save()

        # Circulars, and a certificate the office issued earlier.
        for title, subtitle, days_ago in ((f"PTM notice · {self.ptm_long}", "Circular 14 · slots and timings", 4), ("Half-yearly exam timetable", "Grades 6–8 · 12–23 October", 2)):
            pdf = _pdf(title, subtitle)
            d = Document(kind=Document.Kind.CIRCULAR, title=title, subtitle=subtitle, size=len(pdf), audience=Document.Audience.FAMILIES, owner=principal, issued_on=today - timedelta(days=days_ago))
            d.file.save(title.replace(" ", "_") + ".pdf", ContentFile(pdf), save=False)
            d.save()
        pdf = _pdf("Term 2 report card 2025–26", "Grade 5-B · Aarav Sharma")
        old = Document(kind=Document.Kind.REPORT_CARD, title="Term 2 report card · 2025–26", subtitle="Grade 5-B · 88% · A", size=len(pdf), audience=Document.Audience.STUDENT, student=aarav, owner=principal, issued_on=date(2026, 3, 28))
        old.file.save("Report_card_2025-26_T2.pdf", ContentFile(pdf), save=False)
        old.save()
        _ = CertificateRequest

        # The absence on the register was an approved sick leave.
        absent_day = self._school_days(today, 11)[0]
        LeaveApplication.objects.create(
            student=aarav, from_date=absent_day, to_date=absent_day, kind="sick", reason="Fever — resting at home",
            status="approved", applied_by=parent, decided_by=staff["Priya Menon"], decided_at=timezone.now() - timedelta(days=10),
        )

        # This morning: off Bus 07 at the school gate at 7:52.
        pickup = Trip.objects.filter(route__code="07", service_date=today, direction=Direction.PICKUP).first()
        if pickup is not None and today.weekday() != 6:
            Trip.objects.filter(pk=pickup.pk).update(
                status=Trip.Status.COMPLETED,
                started_at=datetime.combine(today, time(7, 1), tzinfo=tz),
                ended_at=datetime.combine(today, time(7, 53), tzinfo=tz),
            )
            BoardingEvent.objects.create(trip=pickup, student=aarav, kind="boarded", at=datetime.combine(today, time(7, 19), tzinfo=tz))
            BoardingEvent.objects.create(trip=pickup, student=aarav, kind="dropped", at=datetime.combine(today, time(7, 52), tzinfo=tz))

    def _seed_student_extras(self, school, groups, subjects, staff, aarav, today, tz):
        """Data the Student app screens need (Phase 3)."""
        from django.core.files.base import ContentFile

        from apps.learning.models import (
            Assignment,
            AssignmentGroup,
            AssignmentMilestone,
            AssignmentSubmission,
            MilestoneProgress,
            StudyMaterial,
            SyllabusProgress,
        )
        from apps.results.models import ExamPaper, PrepItem

        g6b = groups["6-B"]
        a = self.assignments["6-B"]
        now = timezone.now()

        # Syllabus covered so far, per subject (the "shelf").
        for code, pct, topic in (("MATH", 68, "Fractions"), ("ENG", 72, ""), ("SCI", 64, ""), ("HIN", 70, ""), ("SST", 58, ""), ("CS", 75, ""), ("ART", 60, ""), ("PE", 65, "")):
            SyllabusProgress.objects.create(class_group=g6b, subject=subjects[code], percent=pct, current_topic=topic, updated_by=a[code])

        # Study material: four new this week, three from earlier in the month.
        for code, kind, title, desc, days, pages, minutes in (
            ("MATH", "notes", "Fractions — visual models", "Fraction bars for halves, thirds, quarters and sixths.", 1, 6, None),
            ("MATH", "video", "Adding unlike fractions", "", 1, None, 12),
            ("SCI", "slides", "Water purification methods", "", 3, 18, None),
            ("ENG", "worksheet", "Diary entry format", "", 4, 2, None),
            ("SST", "map", "Rivers of India map", "", 7 + 4, 1, None),
            ("CS", "notes", "Scratch: loops and events", "", 7 + 9, 4, None),
            ("SCI", "video", "Separation of substances", "", 7 + 11, None, 9),
        ):
            m = StudyMaterial(class_group=g6b, subject=subjects[code], kind=kind, title=title, description=desc, author=a[code], pages=pages, duration_minutes=minutes, published_at=now - timedelta(days=days))
            if kind == "video":
                m.url = "https://example.org/lessons/" + title.lower().replace(" ", "-")
            else:
                pdf = _pdf(title, desc or subjects[code].name)
                m.size = len(pdf)
                m.file.save(title.replace(" ", "_").replace("—", "-") + ".pdf", ContentFile(pdf), save=False)
            m.save()

        # Group project: water filtration model (Aarav, Meera, Vihaan, Tara).
        names = {s.full_name: s for s in Student.objects.filter(class_group=g6b, full_name__in=["Meera Pillai", "Vihaan Joshi", "Tara Menon"])}
        project = Assignment.objects.create(
            class_group=g6b, subject=subjects["SCI"], kind="project", title="Water filtration model",
            description="Build a working model that cleans muddy water, and present how each layer helps.",
            group_size=4, max_marks=20, due_date=self._school_day(today, 11), created_by=a["SCI"],
            rubric=[{"key": "working", "label": "Model works", "max": 8}, {"key": "science", "label": "Science explained", "max": 8}, {"key": "presentation", "label": "Presentation", "max": 4}],
        )
        group = AssignmentGroup.objects.create(assignment=project, name="Group 3")
        members = [aarav, names["Meera Pillai"], names["Vihaan Joshi"], names["Tara Menon"]]
        group.members.add(*members)
        plan = (
            ("Research", -8, [aarav, names["Meera Pillai"]], True),
            ("Materials list", -4, [names["Vihaan Joshi"]], True),
            ("Build the model", 5, [aarav, names["Vihaan Joshi"]], False),
            ("Report & presentation", 11, [names["Tara Menon"], names["Meera Pillai"]], False),
        )
        for order, (title, offset, owners, done) in enumerate(plan, start=1):
            milestone = AssignmentMilestone.objects.create(assignment=project, title=title, due_date=self._school_day(today, offset), order=order)
            progress = MilestoneProgress.objects.create(group=group, milestone=milestone, done_at=now - timedelta(days=-offset) if done else None)
            progress.owners.add(*owners)

        # Graded individual work and one that opens next week.
        scratch = Assignment.objects.create(
            class_group=g6b, subject=subjects["CS"], title="Scratch animation", description="A 20-second story told with loops and broadcasts.",
            max_marks=20, due_date=today - timedelta(days=15), created_by=a["CS"],
        )
        AssignmentSubmission.objects.create(
            assignment=scratch, student=aarav, submitted_by=aarav.user, submitted_at=now - timedelta(days=15), status="graded",
            total=18, grade="A+", graded_by=a["CS"], graded_at=now - timedelta(days=9),
            feedback="Great use of loops and broadcast messages — the story flows well. Next time, add sound cues so each scene change is clearer.",
        )
        Assignment.objects.create(
            class_group=g6b, subject=subjects["ENG"], title="Book review", description="Review a book you read this term in 300 words.", teaser="300 words",
            max_marks=20, opens_on=self._school_day(today, 5), due_date=self._school_day(today, 16), created_by=a["ENG"],
        )

        # More marked homework for "Marked & returned".
        for code, title, grade, days in (("MATH", "Ex 7.2, Fractions", "A+", 5), ("ENG", "Vocabulary journal", "A", 12)):
            hw = Homework.objects.create(
                class_group=g6b, subject=subjects[code], title=title, description="", assigned_by=a[code],
                assigned_on=self._school_day(today, -days - 3), due_date=self._school_day(today, -days), accepts_photos=True,
            )
            HomeworkSubmission.objects.create(homework=hw, student=aarav, submitted_by=aarav.user, submitted_at=now - timedelta(days=days), status="reviewed", grade=grade)

        # The half-yearly date sheet for 6-B: six papers, every other school day.
        exam = Exam.objects.get(class_group=g6b, name="Half-yearly")
        first = self._school_day(today, 17)
        exam.held_on = first
        exam.admit_cards_from = self._school_day(first, -6)
        exam.report_by = time(8, 45)
        exam.save()
        papers = (
            ("ENG", ["Ch 1–5", "Poems 1–4", "Grammar"]),
            ("MATH", ["Ch 1–7", "Fractions", "Integers"]),
            ("SCI", ["Ch 1–8", "Lab expts 1–5"]),
            ("HIN", ["Ch 1–7", "Letters", "Grammar"]),
            ("SST", ["History 1–4", "Geo 1–3", "Map work"]),
            ("CS", ["Scratch basics", "Online safety"]),
        )
        for i, (code, syllabus) in enumerate(papers):
            ExamPaper.objects.create(exam=exam, subject=subjects[code], date=self._school_day(first, i * 2), starts_at=time(9, 0), ends_at=time(11, 30), room="Room 204", syllabus=syllabus)
        for offset, title, done in (
            (-1, "Check the date sheet and room", True),
            (0, "Revise Maths Ch 1–6 notes", True),
            (3, "Lab record, Experiment 4", False),
            (8, "Practise the Rivers of India map", False),
            (11, "Collect admit card", False),
            (15, "One past paper per subject", False),
        ):
            PrepItem.objects.create(exam=exam, student=aarav, title=title, due_date=self._school_day(today, offset), done_at=now if done else None)

        # Aarav's own chats with his teachers.
        for teacher, label, thread in (
            (staff["Priya Menon"], "Class teacher · Mathematics", [(aarav.user, "Ma'am, for Ex 7.3 should we draw fraction models?", timedelta(hours=3)), (staff["Priya Menon"], "Yes, draw models for Q4 and Q9.", timedelta(hours=1))]),
            (staff["Joseph Thomas"], "Science · Re: Lab record, Experiment 4", [(staff["Joseph Thomas"], "Please finish the observation table for Experiment 4.", timedelta(days=2)), (aarav.user, "I'll submit it on Friday, sir.", timedelta(days=1, hours=20))]),
            (staff["Nikhil Rao"], "Computer Science", [(staff["Nikhil Rao"], "Your Scratch animation is graded.", timedelta(days=9))]),
        ):
            c = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=aarav)
            ConversationMember.objects.create(conversation=c, user=aarav.user, side="family", label="Student")
            ConversationMember.objects.create(conversation=c, user=teacher, side="staff", label=label)
            self._messages(c, thread, now)
            unread = teacher == staff["Priya Menon"]
            ConversationMember.objects.filter(conversation=c, user=aarav.user).update(last_read_at=now - timedelta(hours=2) if unread else now)
            ConversationMember.objects.filter(conversation=c, user=teacher).update(last_read_at=now)

    def _seed_staff_extras(self, school, groups, subjects, staff, principal, aarav, today, tz):
        """Data the Staff app screens need (Phase 4): Priya Menon's day, desk, leave, chats and files."""
        import io

        from django.core.files.base import ContentFile
        from PIL import Image

        from apps.academics.models import TimetableSlot
        from apps.documents.models import Document
        from apps.learning.models import Assignment, AssignmentSubmission, SubmissionFile, SyllabusProgress
        from apps.messaging.models import Meeting
        from apps.results.models import ExamPaper, MarkSheet
        from apps.staff.models import StaffLeave, StaffProfile, Substitution

        priya, kavya, joseph = staff["Priya Menon"], staff["Kavya Nair"], staff["Joseph Thomas"]
        now = timezone.now()
        at = lambda day, h, m: datetime.combine(day, time(h, m), tzinfo=tz)  # noqa: E731

        # --- HR profiles --------------------------------------------------------
        for i, (name, _role, title, _codes, _dept) in enumerate(NAMED_STAFF):
            StaffProfile.objects.create(
                user=staff[name],
                employee_id="SPS-T-0142" if name == "Priya Menon" else f"SPS-T-{150 + i:04d}",
                designation=title.split(" · ")[0],
                joined_on=date(2018, 6, 1) if name == "Priya Menon" else date(2015 + i % 9, 6, 1),
            )

        # --- Priya's leave this year: CL 6 of 12, SL 5 of 8, EL 10 of 15 left, one earned request pending ---
        for kind, start, end, days, reason, state, note, sent in (
            ("casual", date(2026, 4, 20), date(2026, 4, 24), 5, "Family function in Thrissur", "approved", "", date(2026, 4, 6)),
            ("earned", date(2026, 5, 11), date(2026, 5, 15), 5, "Summer travel", "approved", "", date(2026, 4, 20)),
            ("sick", date(2026, 6, 15), date(2026, 6, 17), 3, "Viral fever", "approved", "", date(2026, 6, 15)),
            ("casual", date(2026, 7, 9), date(2026, 7, 9), 1, "Bank and passport appointments", "approved", "", date(2026, 7, 2)),
            ("casual", date(2026, 8, 14), date(2026, 8, 14), 1, "Personal work", "declined", "Clashes with I-Day event", date(2026, 8, 3)),
            ("earned", date(2026, 11, 16), date(2026, 11, 18), 3, "Sister's wedding in Kochi", "pending", "", today - timedelta(days=8)),
        ):
            leave = StaffLeave.objects.create(
                user=priya, kind=kind, from_date=start, to_date=end, days=days, reason=reason, status=state, decision_note=note,
                decided_by=principal if state != "pending" else None,
                decided_at=at(sent + timedelta(days=1), 10, 30) if state != "pending" else None,
            )
            StaffLeave.objects.filter(pk=leave.pk).update(created_at=at(sent, 9, 15))

        # --- Today's cover: Kavya Nair is on leave; Priya takes her 6-C English period ---
        if today.weekday() < 6:
            weekday = today.weekday()
            periods = sorted(TimetableSlot.objects.filter(class_group=groups["6-C"], weekday=weekday).values_list("period", flat=True))
            busy = set(TimetableSlot.objects.filter(teacher=priya, weekday=weekday).values_list("period", flat=True))
            free = [p for p in periods if p not in busy]
            period = 6 if 6 in free else (free[-1] if free else None)
            if period:
                slot = TimetableSlot.objects.get(class_group=groups["6-C"], weekday=weekday, period=period)
                slot.subject, slot.teacher, slot.room = subjects["ENG"], kavya, "Room 207"
                slot.save(update_fields=["subject", "teacher", "room"])
                StaffLeave.objects.create(
                    user=kavya, kind="casual", from_date=today, to_date=today, days=1, reason="Unwell", status="approved",
                    decided_by=principal, decided_at=at(today, 7, 50),
                )
                Substitution.objects.create(
                    date=today, slot=slot, teacher=priya, absent_teacher=kavya, reason="on leave", assigned_by=principal, assigned_at=at(today, 8, 5)
                )

        # --- Maths unit-test averages by section (StaffClasses) --------------------
        math = subjects["MATH"]
        for key, targets in {"6-B": (78, 81), "6-A": (74, 76), "7-A": (75, 74), "7-C": (72, 74)}.items():
            for name, target in zip(("Unit Test 1", "Unit Test 2"), targets):
                rows = list(ExamMark.objects.filter(exam__class_group=groups[key], exam__name=name, subject=math))
                for _ in range(3):
                    avg = sum(float(r.marks) for r in rows) / len(rows)
                    shift = target - avg
                    for r in rows:
                        if r.student_id != aarav.id:
                            r.marks = Decimal(max(20, min(100, round(float(r.marks) + shift))))
                ExamMark.objects.bulk_update(rows, ["marks"])

        # --- 7-C Unit Test 2 marks are still open: out of 40, 30 of 34 in, due Thursday ---
        ut2 = Exam.objects.get(class_group=groups["7-C"], name="Unit Test 2")
        ut2.is_published, ut2.published_on = False, None
        ut2.save(update_fields=["is_published", "published_on"])
        due = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
        MarkSheet.objects.create(exam=ut2, subject=math, max_marks=40, due_on=due, saved_at=now - timedelta(minutes=12), saved_by=priya)
        roster = {s.full_name: s for s in Student.objects.filter(class_group=groups["7-C"])}
        fixed = {"Aditi Rao": 34, "Arnav Bhatt": 28, "Bhavya Shetty": 31, "Farhan Qureshi": 22, "Gauri Kulkarni": 37, "Karan Anand": 36, "Lavanya Pillai": 26, "Nandini Hegde": 30, "Rohan Gupta": 33}
        blanks = {"Mihir Desai", "Pranav Shah"}
        later = sorted(roster.values(), key=lambda s: s.roll_no)[12:]
        blanks.update(s.full_name for s in later[-2:])
        rows = list(ExamMark.objects.filter(exam=ut2, subject=math).select_related("student"))
        for r in rows:
            name = r.student.full_name
            r.max_marks = Decimal(40)
            r.marks = Decimal(fixed.get(name, round(float(r.marks) * 0.4)))
            r.is_absent = name == "Ishita Dutta"
            if r.is_absent:
                r.marks = Decimal(0)
        ExamMark.objects.bulk_update(rows, ["marks", "max_marks", "is_absent"])
        ExamMark.objects.filter(exam=ut2, subject=math, student__full_name__in=blanks).delete()

        # --- Syllabus covered in Priya's other sections -----------------------------
        for key, pct, topic in (("6-A", 61, "Fractions · Ex 7.2"), ("7-A", 58, "Integers · Ex 2.3"), ("7-C", 55, "Integers · Ex 2.1")):
            SyllabusProgress.objects.create(class_group=groups[key], subject=math, percent=pct, current_topic=topic, updated_by=priya)

        # --- Homework: 12 of 38 have handed in Ex 7.3 (6-B) ------------------------------
        ex73 = Homework.objects.filter(class_group=groups["6-B"], subject=math).order_by("-due_date").first()
        for s in Student.objects.filter(class_group=groups["6-B"]).exclude(id=aarav.id).order_by("roll_no")[2:14]:
            HomeworkSubmission.objects.create(homework=ex73, student=s, submitted_by=None, submitted_at=now - timedelta(hours=random.randint(1, 20)))

        # --- The 6-B Maths project Priya is marking (StaffAssignments) ---------------------
        project = Assignment.objects.create(
            class_group=groups["6-B"], subject=math, kind=Assignment.Kind.ASSIGNMENT, title="Fractions in daily life",
            description="Poster or short report", teaser="Poster or short report", max_marks=20, group_size=1,
            rubric=[
                {"key": "concept", "label": "Concept", "hint": "Correct use of fractions", "max": 10},
                {"key": "presentation", "label": "Presentation", "hint": "Neat, labelled, readable", "max": 5},
                {"key": "creativity", "label": "Creativity", "hint": "Original, real-life examples", "max": 5},
            ],
            due_date=self._school_day(today, 8), created_by=priya,
        )
        buf = io.BytesIO()
        Image.new("RGB", (640, 480), (245, 214, 170)).save(buf, "JPEG", quality=70)
        poster = buf.getvalue()
        class6b = list(Student.objects.filter(class_group=groups["6-B"]).order_by("roll_no"))
        by_name = {s.full_name: s for s in class6b}
        to_review = [
            ("Ananya Iyer", ["kitchen-fractions-poster.jpg"], 5),
            ("Aarav Sharma", ["recipe-fractions.pdf"], 4),
            ("Meera Pillai", ["market-survey.pdf"], 4),
            ("Rohan Iyer", ["pizza-model.jpg", "pizza-model-2.jpg"], 3),
            ("Saanvi Reddy", ["fraction-wall.pdf"], 3),
        ]
        rest = [s for s in class6b if s.full_name not in dict((n, 1) for n, _f, _d in to_review)]
        to_review += [(s.full_name, [f"{s.first_name.lower()}-fractions.pdf"], 2) for s in rest[:4]]
        graded = rest[4:18]
        feedback = [
            "Clear examples and neat labels. Simplify 4/8 next time.",
            "Good real-life cases. Show the working beside each picture.",
            "Creative idea! A few fractions need simplifying.",
        ]
        for name, files, days_ago in to_review:
            sub = AssignmentSubmission.objects.create(
                assignment=project, student=by_name[name], submitted_by=by_name[name].user, submitted_at=at(today - timedelta(days=days_ago), 18, 42)
            )
            for f in files:
                content = poster if f.endswith(".jpg") else _pdf(f.removesuffix(".pdf").replace("-", " ").title(), "Fractions in daily life")
                SubmissionFile.objects.create(submission=sub, file=ContentFile(content, name=f), name=f, size=len(content))
        for i, s in enumerate(graded):
            concept, presentation, creativity = random.randint(6, 10), random.randint(3, 5), random.randint(3, 5)
            total = concept + presentation + creativity
            sub = AssignmentSubmission.objects.create(
                assignment=project, student=s, submitted_at=at(today - timedelta(days=6), 17, 0), status=AssignmentSubmission.Status.GRADED,
                scores={"concept": concept, "presentation": presentation, "creativity": creativity}, total=total,
                grade=grade_for(total * 5, school), feedback=feedback[i % 3], graded_by=priya, graded_at=now - timedelta(days=2),
            )
            content = _pdf(f"{s.first_name} · Fractions in daily life", "Poster")
            SubmissionFile.objects.create(submission=sub, file=ContentFile(content, name=f"{s.first_name.lower()}-poster.pdf"), name=f"{s.first_name.lower()}-poster.pdf", size=len(content))

        # --- Messages to Priya ------------------------------------------------------------
        def guardian(student_name, full_name):
            link = StudentGuardian.objects.get(student=by_name[student_name])
            User.objects.filter(pk=link.user_id).update(full_name=full_name)
            link.user.refresh_from_db()
            return link.user

        rahul_chat = Conversation.objects.filter(kind=Conversation.Kind.DIRECT, student=aarav, members__user=priya).filter(members__user=self.parent).first()
        self._messages(rahul_chat, [(self.parent, "Thank you for the note on Aarav's fractions work. Could you suggest a few more practice sums for the weekend?", timedelta(minutes=10))], now)
        ConversationMember.objects.filter(conversation=rahul_chat, user=priya).update(last_read_at=now - timedelta(hours=1))

        ptm_slots = [(10, 30), (10, 45), (11, 0), (11, 15)]
        for i, (student_name, parent_name) in enumerate((("Ananya Iyer", "Neha Iyer"), ("Rohan Iyer", "Suresh Iyer"), ("Saanvi Reddy", "Kavitha Reddy"), ("Meera Pillai", "Anil Pillai"))):
            parent_user = guardian(student_name, parent_name)
            student = by_name[student_name]
            chat = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=student)
            ConversationMember.objects.create(conversation=chat, user=parent_user, side="family", label=f"Parent of {student.first_name}")
            ConversationMember.objects.create(conversation=chat, user=priya, side="staff", label="Class teacher · 6-B")
            self._messages(chat, [(parent_user, "Requested a parent–teacher meeting slot.", timedelta(hours=1, minutes=30 + i * 50))], now)
            h, m = ptm_slots[i]
            Meeting.objects.create(
                conversation=chat, title="Parent–teacher meeting", starts_at=at(self.ptm, h, m), ends_at=at(self.ptm, h, m) + timedelta(minutes=15),
                location="Room 204", status=Meeting.Status.REQUESTED, booked_by=parent_user,
            )
            ConversationMember.objects.filter(conversation=chat, user=priya).update(last_read_at=now - timedelta(minutes=5) if i else None)

        imran = guardian("Kabir Khan", "Imran Khan")
        chat = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=by_name["Kabir Khan"])
        ConversationMember.objects.create(conversation=chat, user=imran, side="family", label="Parent of Kabir")
        ConversationMember.objects.create(conversation=chat, user=priya, side="staff", label="Class teacher · 6-B")
        self._messages(chat, [(imran, "Kabir has a fever and won't come today. I'll send the leave note with him tomorrow.", max(timedelta(minutes=5), now - at(today, 7, 41)))], now)
        ConversationMember.objects.filter(conversation=chat).update(last_read_at=now)

        chat = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=None)
        ConversationMember.objects.create(conversation=chat, user=joseph, side="staff", label="Science")
        ConversationMember.objects.create(conversation=chat, user=priya, side="staff", label="Mathematics")
        self._messages(chat, [(joseph, "Could we swap Thursday P3? 6-B needs the lab to redo Experiment 4. I'll take any period you like.", max(timedelta(minutes=50), now - at(today, 10, 15)))], now)
        ConversationMember.objects.filter(conversation=chat).update(last_read_at=now)

        # --- The principal's invigilation notice (StaffHome) --------------------------------
        first = ExamPaper.objects.filter(exam__class_group=groups["6-B"], exam__name="Half-yearly").order_by("date").values_list("date", flat=True)
        duties = list(first[:3])
        if duties:
            confirm = self._school_day(today, 2)
            Announcement.objects.filter(title="Half-yearly invigilation roster").update(
                body=(
                    f"Half-yearly exams begin {duties[0]:%a} {duties[0].day} {duties[0]:%b}. Your duties: "
                    f"{', '.join(str(d.day) for d in duties[:-1])} and {duties[-1].day} {duties[-1]:%b}, Hall B, 9:00 AM. "
                    f"Please confirm by {confirm:%a} {confirm.day} {confirm:%b}."
                ),
            )

        # --- Priya's documents -----------------------------------------------------------
        def doc(kind, title, subtitle, audience, days_ago, **extra):
            content = _pdf(title, subtitle)
            d = Document.objects.create(
                kind=kind, title=title, subtitle=subtitle, file=ContentFile(content, name=f"{title.lower().replace(' ', '-')[:40]}.pdf"),
                size=len(content), audience=audience, owner=extra.pop("owner", priya), issued_on=today - timedelta(days=days_ago), **extra,
            )
            Document.objects.filter(pk=d.pk).update(created_at=now - timedelta(days=days_ago))
            return d

        doc("lesson_plan", "Fractions — unit plan", "6-A, 6-B", Document.Audience.STAFF, 11, subject=math)
        doc("lesson_plan", "Integers — unit plan", "7-A, 7-C", Document.Audience.STAFF, 24, subject=math)
        doc("question_paper", "UT2 Class 7 paper + key", "Sent to exam cell", Document.Audience.PRIVATE, 16, status="accepted")
        doc("question_paper", "Half-yearly · Set B", "Class 6", Document.Audience.PRIVATE, 5, status="accepted", locked_until=today + timedelta(days=25))
        doc("circular", "Invigilation roster", "Half-yearly exams", Document.Audience.STAFF, 0, owner=principal)
        doc("circular", f"PTM guidelines · {self.ptm_short}", "Slots, room plan", Document.Audience.STAFF, 8, owner=principal)
        for months_ago, label in ((1, "August 2026"), (2, "July 2026")):
            doc("payslip", f"Payslip · {label}", "Issued by HR", Document.Audience.PRIVATE, 25 + 30 * (months_ago - 1))
        doc("certificate", "B.Ed. degree certificate", "Uploaded Jun 2018", Document.Audience.PRIVATE, 3000, status="verified")
        doc("certificate", "CBSE workshop · Maths lab", "Workshop certificate", Document.Audience.PRIVATE, 14, status="in_review")

    def _seed_principal_extras(self, school, groups, subjects, staff, principal, today, tz):
        """Data the principal's phone needs (Phase 5): who's on leave, the in-tray, chronic absentees."""
        from django.core.files.base import ContentFile

        from apps.admissions.models import Application
        from apps.approvals.models import ApprovalEvent, ApprovalRequest
        from apps.approvals.services import open_request
        from apps.attendance.models import AttendanceException, AttendanceSession
        from apps.fees.models import FeeInvoice, Payment, Refund
        from apps.results.models import Exam, ExamMark, MarkCorrection
        from apps.staff.models import StaffLeave

        now = timezone.now()
        at = lambda day, h, m: datetime.combine(day, time(h, m), tzinfo=tz)  # noqa: E731
        tomorrow = self._school_day(today, 1)
        in_two = self._school_day(today, 2)

        def aged(req, when):
            ApprovalRequest.objects.filter(pk=req.pk).update(created_at=when)
            ApprovalEvent.objects.filter(request=req).update(created_at=when)

        # --- Teachers on leave today: Kavya (seeded with the cover), Vikram (medical), Sunita (casual) ---
        if today.weekday() < 6:
            for name, kind, reason in (("Vikram Singh", "sick", "Fever"), ("Sunita Verma", "casual", "Family function")):
                StaffLeave.objects.create(
                    user=staff[name], kind=kind, from_date=today, to_date=today, days=1, reason=reason, status="approved",
                    decided_by=principal, decided_at=at(today, 7, 40),
                )

        # --- The in-tray: 5 leave, 3 marks corrections, 2 refunds, 2 admissions ---
        vikram = StaffLeave.objects.create(
            user=staff["Vikram Singh"], kind="sick", from_date=tomorrow, to_date=tomorrow, days=1,
            reason="Extends today's medical leave by a day.",
            certificate=ContentFile(_pdf("Medical certificate", "Viral fever · rest advised for two days"), name="medical-certificate.pdf"),
            certificate_name="Medical certificate.pdf",
        )
        aged(open_request(kind="leave", target=vikram, requested_by=staff["Vikram Singh"], summary=f"Sick · {tomorrow:%a %d %b} · 1 day", due_on=tomorrow, notify_principal=False), at(today, 8, 5))
        kavya = StaffLeave.objects.create(
            user=staff["Kavya Nair"], kind="casual", from_date=tomorrow, to_date=in_two, days=2, reason="Personal work at home in Thrissur.",
        )
        aged(open_request(kind="leave", target=kavya, requested_by=staff["Kavya Nair"], summary=f"Casual · {tomorrow:%d}–{in_two:%d %b} · 2 days", due_on=tomorrow, notify_principal=False), now - timedelta(days=1, hours=5))
        ravi_day = self._school_day(today, 6)
        ravi = StaffLeave.objects.create(user=staff["Ravi Kumar"], kind="casual", from_date=ravi_day, to_date=ravi_day, days=1, reason="Bank appointment")
        aged(open_request(kind="leave", target=ravi, requested_by=staff["Ravi Kumar"], summary=f"Casual · {ravi_day:%a %d %b} · 1 day", due_on=ravi_day, notify_principal=False), now - timedelta(hours=26))
        deepa_day = self._school_day(today, 9)
        deepa = StaffLeave.objects.create(user=staff["Deepa Iyer"], kind="earned", from_date=deepa_day, to_date=self._school_day(today, 10), days=2, reason="Cousin's wedding")
        aged(open_request(kind="leave", target=deepa, requested_by=staff["Deepa Iyer"], summary=f"Earned · {deepa_day:%d %b} · 2 days", due_on=deepa_day, notify_principal=False), now - timedelta(days=2))
        for leave in StaffLeave.objects.filter(user=staff["Priya Menon"], status="pending"):
            aged(open_request(kind="leave", target=leave, requested_by=leave.user, summary=f"Earned · {leave.from_date:%d}–{leave.to_date:%d %b} · {int(leave.days)} days", due_on=leave.from_date, notify_principal=False), now - timedelta(days=8))

        def correction(teacher, key, code, changes, reason, checker, hours_ago):
            exam = Exam.objects.get(class_group=groups[key], name="Unit Test 2")
            students = list(Student.objects.filter(class_group=groups[key]).order_by("roll_no"))
            entries = []
            for roll, delta, note in changes:
                s = students[roll - 1]
                mark = ExamMark.objects.get(exam=exam, student=s, subject=subjects[code])
                entries.append({"student_id": str(s.id), "from": float(mark.marks), "to": float(min(mark.max_marks, mark.marks + delta)), "note": note})
            corr = MarkCorrection.objects.create(exam=exam, subject=subjects[code], reason=reason, checked_by=checker, entries=entries)
            aged(open_request(kind="marks", target=corr, requested_by=teacher, summary=f"UT2 · {key} {subjects[code].name} · {len(entries)} change{'s' if len(entries) != 1 else ''}", notify_principal=False), now - timedelta(hours=hours_ago))

        correction(
            staff["Joseph Thomas"], "8-A", "SCI", [(3, 4, "Q7 · 0 → 4"), (9, 4, "Q7 · 0 → 4"), (26, 4, "Q7 · 0 → 4")],
            "The key had (b) for Q7. The right answer is (c), so the three children who wrote (c) lost 4 marks each.",
            staff["Meera Krishnan"], 2,
        )
        correction(staff["Priya Menon"], "6-A", "MATH", [(12, 2, "Q4 · total added wrong")], "Q4 was marked 3 but the working earns 5; the total was added wrong.", None, 20)
        correction(staff["Nikhil Rao"], "6-B", "CS", [(5, 3, "Q2 · missed page"), (17, 2, "Q9 · missed page")], "Two answer sheets had a page I missed while marking.", None, 30)

        rohan = self.students[("7-C", "Rohan Gupta")]
        meena = self._user(self._phone(1900), "Meena Gupta", school, Role.PARENT, title="Parent")
        StudentGuardian.objects.create(student=rohan, user=meena, relationship="mother", is_primary=True)
        paid_on = today - timedelta(days=8)
        inv = self._invoice(school, rohan, "Transport · Q3", "transport", 6000, paid_on, True, paid_on, meena, tz)
        Payment.objects.filter(invoice=inv).update(method="upi")
        refund = Refund.objects.create(payment=inv.payments.first(), amount=6000, fee_head="Transport Q3", reason="The family moved house; the new address isn't on any bus route from 1 Oct.", asked_by=meena)
        aged(open_request(kind="refund", target=refund, requested_by=User.objects.get(phone=self._phone(40)), summary="Rohan Gupta · 7-C · ₹6,000", notify_principal=False), now - timedelta(days=6))
        other = Student.objects.filter(class_group=groups["5-A"]).order_by("roll_no").first()
        inv2 = self._invoice(school, other, "Excursion · Science centre", "other", 2500, today - timedelta(days=20), True, today - timedelta(days=20), None, tz)
        Payment.objects.filter(invoice=inv2).update(method="card")
        refund2 = Refund.objects.create(payment=inv2.payments.first(), amount=2500, fee_head="Excursion", reason="The science-centre trip was cancelled by the venue.")
        aged(open_request(kind="refund", target=refund2, requested_by=User.objects.get(phone=self._phone(40)), summary=f"{other.full_name} · 5-A · ₹2,500", notify_principal=False), now - timedelta(days=3))

        sibling = Student.objects.filter(class_group=groups["4-A"]).order_by("roll_no").first()
        Student.objects.filter(pk=sibling.pk).update(full_name=f"{sibling.full_name.split()[0]} Kapoor")
        sibling.refresh_from_db()
        school.settings = {**(school.settings or {}), "admissions": {"seats": {"1": 14, "6": 3}}, "fees": {"target": {"percent": 90, "by": self._school_day(today, 4).isoformat()}}}
        school.save(update_fields=["settings"])
        for no, child, grade, verified, interaction, sib in (
            ("APP-27-0412", "Ishita Kapoor", "1", True, today - timedelta(days=8), sibling),
            ("APP-27-0398", "Vivaan Bhatia", "6", True, today - timedelta(days=5), None),
        ):
            app = Application.objects.create(
                application_no=no, child_name=child, grade=grade, academic_year="2027–28", documents_verified=verified, interaction_on=interaction, sibling=sib,
            )
            aged(open_request(kind="admission", target=app, requested_by=User.objects.get(phone=self._phone(43)), summary=f"Grade {grade} · 2027–28 · {no}", notify_principal=False), now - timedelta(days=4))

        # --- Six chronic absentees: absent every day for the last 3–5 school days, no reason given ---
        days = self._school_days(today, 4) + [today]
        for key, name, run in (
            ("9-B", "Harsh Vardhan", 5), ("9-A", "Pooja Nambiar", 4), ("7-C", "Samar Khanna", 4),
            ("7-A", "Divya Krishnan", 3), ("4-B", "Kunal Mehta", 3), ("4-A", "Fatima Sayed", 3),
        ):
            s = Student.objects.filter(class_group=groups[key]).exclude(user__isnull=False).order_by("-roll_no").first()
            Student.objects.filter(pk=s.pk).update(full_name=name)
            if not StudentGuardian.objects.filter(student=s).exists():
                g = self._user(self._phone(1950 + len(name) + run * 7 + list(groups).index(key)), f"{random.choice(FIRST)} {name.split()[-1]}", school, Role.PARENT, title="Parent")
                StudentGuardian.objects.create(student=s, user=g, relationship="parent", is_primary=True)
            for d in days[-run:]:
                session = AttendanceSession.objects.filter(class_group=groups[key], date=d).first()
                if session:
                    AttendanceException.objects.update_or_create(session=session, student=s, defaults={"status": "absent", "note": ""})
            # The day before the run, they were in.
            before = self._school_days(today, run + 1)[0]
            AttendanceException.objects.filter(session__class_group=groups[key], session__date=before, student=s).delete()

    def _messages(self, conversation, thread, now):
        last = None
        start = Message.objects.filter(conversation=conversation).count()
        for index, (sender, body, ago) in enumerate(thread, start=start):
            message = Message.objects.create(conversation=conversation, sender=sender, body=body, client_id=f"design-{conversation.id}-{index}")
            Message.objects.filter(pk=message.pk).update(created_at=now - ago)
            last = (now - ago, body)
        Conversation.objects.filter(pk=conversation.pk).update(last_message_at=last[0], last_message_preview=last[1][:140])

    def _seed_notifications(self, school, parent, today):
        now = timezone.now()
        items = [
            ("transport", "Bus 07 is running 12 min late", "Held at the ORR junction. New drop time 3:17 PM.", 0.1, False),
            ("homework", "Maths homework: Exercise 7.3", "Due tomorrow, P1 · Priya Menon", 5, False),
            ("announcement", f"Parent–teacher meeting · {self.ptm_short}", "Pick a 15-minute slot with Ms. Menon.", 20, False),
            ("attendance", "Aarav checked in at 7:52 AM", "Off Bus 07 at the school gate.", 6.3, True),
            ("fees", f"Term 2 fee due on {self.due_t2.day} {self.due_t2:%b}", "₹42,500 for Aarav and ₹38,000 for Diya · pay in the app.", 26, True),
            ("homework", "English homework: Diary entry", "Due Thursday · Kavya Nair", 28, True),
            ("results", "Unit Test 2 Science marks published", "Aarav scored 85%.", 80, True),
            ("general", "Lab record reminder from Joseph Thomas", "Experiments 3 and 4 are incomplete.", 140, True),
        ]
        for category, title, body, hours_ago, read in items:
            n = Notification.objects.create(
                user=parent, school=school, category=category, title=title, body=body, push_status="skipped",
                dedupe_key=f"design:{title}", read_at=now if read else None,
            )
            Notification.objects.filter(pk=n.pk).update(created_at=now - timedelta(hours=hours_ago))

    def _print_summary(self):
        self.stdout.write(self.style.SUCCESS("\nDesign data ready. School code: SUNRISE (Sunrise Public School)"))
        rows = [
            ("Parent · Rahul Sharma", "+91 98450 34521"),
            ("Student · Aarav Sharma (6-B)", "+91 98400 00003"),
            ("Teacher · Priya Menon (6-B)", "+91 98400 00010"),
            ("Principal · Dr. Anita Rao", "+91 98400 00001  (password sunrise-principal)"),
            ("Transport desk", "+91 98400 00041"),
            ("Driver · Route 07", "+91 98400 00044"),
        ]
        for label, phone in rows:
            self.stdout.write(f"  {label:<32} {phone}")


def _pdf(title: str, body: str) -> bytes:
    """A small one-page PDF for seeded worksheets, circulars and certificates."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(title)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(60, 780, "Sunrise Public School")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, 750, title)
    c.setFont("Helvetica", 11)
    c.drawString(60, 725, body[:95])
    c.showPage()
    c.save()
    return buf.getvalue()
