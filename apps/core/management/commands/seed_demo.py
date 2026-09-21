"""
Create demo schools with realistic data for local development and demos.

    python manage.py seed_demo            # create (skips if already there)
    python manage.py seed_demo --reset    # wipe the demo schools and recreate them

Sign in with school code GHIS and any demo phone number below; in development
the one-time code is shown in the app (OTP_DEV_ECHO).
"""

import io
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
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
from apps.core.tenant import unscoped, use_school
from apps.core.utils import school_today, school_tz
from apps.fees.models import FeeInvoice, Payment
from apps.fees.services import next_receipt_no
from apps.homework.models import Homework, HomeworkSubmission, SubmissionPhoto
from apps.messaging.models import Conversation, ConversationMember, Message
from apps.notifications.models import Notification
from apps.results.models import Exam, ExamMark
from apps.tenancy.models import Organization, School
from apps.transport.models import Route, Stop, StudentTransport, Vehicle
from apps.transport.services import ensure_trips_for_date, refresh_route_length

DEMO_CODES = ("GHIS", "SPS")

ROUTE_4_PATH = [
    [17.4933, 78.3915],  # depot
    [17.4921, 78.3931],
    [17.4907, 78.3950],
    [17.4895, 78.3968],  # Green Park
    [17.4876, 78.3983],
    [17.4851, 78.3996],
    [17.4822, 78.4006],  # MG Road
    [17.4793, 78.4001],
    [17.4764, 78.3988],
    [17.4740, 78.3972],
    [17.4717, 78.3957],  # Lake View
    [17.4690, 78.3938],
    [17.4662, 78.3918],
    [17.4638, 78.3902],
    [17.4618, 78.3889],  # Silver Oaks Colony
    [17.4593, 78.3865],
    [17.4568, 78.3842],
    [17.4545, 78.3825],
    [17.4526, 78.3812],  # School Gate
]

ROUTE_4_STOPS = [
    # name, lat, lng, pickup offset (min), drop offset (min), is_school
    ("Green Park", 17.4895, 78.3968, 4, 30, False),
    ("MG Road", 17.4822, 78.4006, 9, 24, False),
    ("Lake View", 17.4717, 78.3957, 15, 18, False),
    ("Silver Oaks Colony", 17.4618, 78.3889, 21, 12, False),
    ("School Gate", 17.4526, 78.3812, 28, 0, True),
]

SUBJECTS = [
    ("Mathematics", "MATH", "#2E5D4E"),
    ("Science", "SCI", "#3F6E8C"),
    ("English", "ENG", "#B4763A"),
    ("Hindi", "HIN", "#9C4034"),
    ("Social Studies", "SST", "#6B5B95"),
    ("Computer Science", "CS", "#4E5A66"),
]

ROSTERS = {
    ("8", "B", "Room 214"): [
        "Ananya Reddy", "Arjun Menon", "Aarav Iyer", "Ishaan Gupta", "Kavya Nair", "Neha Joshi",
        "Rohan Verma", "Sanya Kapoor", "Tanvi Rao", "Vihaan Shah", "Zara Khan", "Aditya Kulkarni",
    ],
    ("6", "A", "Room 108"): [
        "Aadhya Singh", "Advait Patil", "Ayaan Qureshi", "Charvi Bose", "Diya Iyer",
        "Dhruv Malhotra", "Myra Chatterjee", "Reyansh Das", "Saanvi Pillai", "Vivaan Mehta",
    ],
    ("10", "C", "Room 305"): [
        "Aisha Siddiqui", "Devansh Agarwal", "Harsh Vardhan", "Ira Deshpande", "Karthik Subramanian",
        "Meher Grewal", "Kabir Sharma", "Nikhil Bhat", "Pooja Srinivasan", "Riya Thomas",
    ],
}

GUARDIAN_FIRST_NAMES = [
    "Lakshmi", "Suresh", "Anjali", "Rajesh", "Sunita", "Vikas", "Kavitha", "Manoj", "Deepa", "Arvind",
    "Shalini", "Prakash", "Rekha", "Sanjay", "Nandini", "Ramesh", "Geeta", "Ajay", "Pallavi", "Naveen",
]

PERIODS = [
    (time(8, 30), time(9, 15)),
    (time(9, 20), time(10, 5)),
    (time(10, 25), time(11, 10)),
    (time(11, 15), time(12, 0)),
    (time(12, 45), time(13, 30)),
    (time(13, 35), time(14, 20)),
]


class Command(BaseCommand):
    help = "Create demo schools (GHIS and SPS) with realistic data."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete and recreate the demo schools")

    def handle(self, *args, **options):
        random.seed(2026)
        with unscoped():
            if options["reset"]:
                self._reset()
            elif School.objects.filter(code="GHIS").exists():
                self.stdout.write("Demo data already exists. Use --reset to recreate it.")
                return
            with transaction.atomic():
                org = Organization.objects.create(name="Greenwood Education Trust", slug="greenwood")
                ghis = School.objects.create(
                    organization=org,
                    code="GHIS",
                    name="Greenwood High International School",
                    short_name="Greenwood High",
                    city="Hyderabad",
                    state="Telangana",
                    languages=["en", "hi", "te"],
                    settings={"payments": {"gateway": "mock"}},
                )
                sunrise_org = Organization.objects.create(name="Sunrise Learning Society", slug="sunrise")
                sps = School.objects.create(
                    organization=sunrise_org,
                    code="SPS",
                    name="Sunrise Public School",
                    short_name="Sunrise",
                    city="Pune",
                    state="Maharashtra",
                    primary_color="#1F4E79",
                    accent_color="#D98E04",
                    languages=["en", "hi"],
                    settings={"payments": {"gateway": "mock"}},
                )
                User.objects.create_superuser("+919000000000", "EduFlow Admin", password="eduflow-admin")
            with transaction.atomic(), use_school(ghis):
                self._seed_greenwood(ghis)
            with transaction.atomic(), use_school(sps):
                self._seed_sunrise(sps)
        self._print_summary()

    # ------------------------------------------------------------------ helpers

    def _reset(self):
        schools = School.objects.filter(code__in=DEMO_CODES)
        Notification.objects.filter(school__in=schools).delete()
        org_ids = list(schools.values_list("organization_id", flat=True))
        schools.delete()
        Organization.objects.filter(id__in=org_ids).delete()
        User.objects.filter(phone__startswith="+9198000").delete()
        User.objects.filter(phone__startswith="+9199000").delete()
        User.objects.filter(phone__startswith="+9199111").delete()
        User.objects.filter(phone="+919000000000").delete()
        self.stdout.write("Removed previous demo data.")

    def _user(self, phone, name, school, role, **membership):
        user = User.objects.filter(phone=phone).first() or User.objects.create_user(phone, name)
        Membership.objects.get_or_create(user=user, role=role, school=school, defaults=membership)
        return user

    # ------------------------------------------------------------------ Greenwood

    def _seed_greenwood(self, school):
        today = school_today(school)
        year = AcademicYear.objects.create(name="2026–27", starts_on=date(2026, 4, 1), ends_on=date(2027, 3, 31), is_current=True)
        subjects = {code: Subject.objects.create(name=name, code=code, color=color) for name, code, color in SUBJECTS}

        # Staff
        principal = self._user("+919800000001", "S. Krishnan", school, Role.PRINCIPAL, title="Principal")
        anita = self._user(
            "+919800000002", "Anita Rao", school, Role.TEACHER, title="Class Teacher · 8B",
            settings={"office_hours": {"days": [0, 1, 2, 3, 4, 5], "start": "15:30", "end": "17:00"}},
        )
        vikram = self._user(
            "+919800000003", "Vikram Das", school, Role.TEACHER, title="Class Teacher · 6A",
            settings={"office_hours": {"days": [0, 1, 2, 3, 4], "start": "14:30", "end": "16:00"}},
        )
        farah = self._user(
            "+919800000004", "Farah Khan", school, Role.TEACHER, title="Class Teacher · 10C",
            settings={"office_hours": {"days": [0, 1, 2, 3, 4, 5], "start": "15:00", "end": "16:30"}},
        )
        priya = self._user("+919800000009", "Priya Nair", school, Role.TEACHER, title="Languages & Social Studies")
        deepak = self._user("+919800000005", "Deepak Nair", school, Role.ACCOUNTANT, title="Accounts", department=Department.ACCOUNTS)
        ravi = self._user("+919800000006", "Ravi Kumar", school, Role.TRANSPORT_MANAGER, title="Transport Manager", department=Department.TRANSPORT)
        suresh = self._user("+919800000007", "Suresh Pillai", school, Role.DRIVER, title="Driver · Bus 4")
        lakshmi = self._user("+919800000008", "Lakshmi Devi", school, Role.ATTENDANT, title="Attendant · Bus 4")
        self._user("+919800000010", "Kavya Reddy", school, Role.ADMIN, title="Front Office", department=Department.OFFICE)

        # Classes
        groups = {}
        for (grade, section, room), _names in ROSTERS.items():
            groups[f"{grade}{section}"] = ClassGroup.objects.create(academic_year=year, grade=grade, section=section)
        groups["8B"].class_teacher = anita
        groups["6A"].class_teacher = vikram
        groups["10C"].class_teacher = farah
        for group in groups.values():
            group.save()

        teaching = {
            "8B": [("MATH", anita), ("SCI", anita), ("ENG", vikram), ("HIN", priya), ("SST", priya)],
            "6A": [("ENG", vikram), ("SCI", farah), ("MATH", farah), ("HIN", priya), ("SST", priya)],
            "10C": [("MATH", anita), ("SCI", farah), ("ENG", vikram), ("HIN", priya), ("SST", priya), ("CS", priya)],
        }
        for key, pairs in teaching.items():
            for code, teacher in pairs:
                TeachingAssignment.objects.create(teacher=teacher, class_group=groups[key], subject=subjects[code])

        rooms = {"8B": "Room 214", "6A": "Room 108", "10C": "Room 305"}
        for key, pairs in teaching.items():
            for weekday in range(6):
                for period, (start, end) in enumerate(PERIODS, start=1):
                    code, teacher = pairs[(weekday + period) % len(pairs)]
                    room = "Science Lab" if code == "SCI" and period % 2 else ("Computer Lab" if code == "CS" else rooms[key])
                    TimetableSlot.objects.create(
                        class_group=groups[key], weekday=weekday, period=period, starts_at=start, ends_at=end,
                        subject=subjects[code], teacher=teacher, room=room,
                    )

        # Families (the two app-using demo families first)
        meera = self._user("+919900000001", "Meera Iyer", school, Role.PARENT, title="Parent")
        rahul = self._user("+919900000002", "Rahul Sharma", school, Role.PARENT, title="Parent")
        kabir_user = self._user("+919900000003", "Kabir Sharma", school, Role.STUDENT, title="Student · 10C")

        students = {}
        guardian_index = 0
        for (grade, section, _room), names in ROSTERS.items():
            group = groups[f"{grade}{section}"]
            for roll, name in enumerate(sorted(names), start=1):
                student = Student.objects.create(
                    full_name=name,
                    admission_no=f"GH{grade.zfill(2)}{section}{roll:03d}",
                    roll_no=roll,
                    class_group=group,
                    user=kabir_user if name == "Kabir Sharma" else None,
                )
                students[name] = student
                if name in {"Aarav Iyer", "Diya Iyer"}:
                    StudentGuardian.objects.create(student=student, user=meera, relationship="mother", is_primary=True)
                elif name == "Kabir Sharma":
                    StudentGuardian.objects.create(student=student, user=rahul, relationship="father", is_primary=True)
                else:
                    surname = name.split()[-1]
                    guardian = self._user(
                        f"+9199000001{guardian_index:02d}",
                        f"{GUARDIAN_FIRST_NAMES[guardian_index % len(GUARDIAN_FIRST_NAMES)]} {surname}",
                        school,
                        Role.PARENT,
                        title="Parent",
                    )
                    guardian_index += 1
                    StudentGuardian.objects.create(student=student, user=guardian, relationship="parent", is_primary=True)

        self._seed_transport(school, students, suresh, lakshmi, today)
        self._seed_attendance(groups, students, anita, vikram, farah, today)
        self._seed_homework(school, groups, subjects, students, anita, vikram, farah, meera, today)
        self._seed_fees(school, students, meera, rahul)
        self._seed_results(groups, subjects, students)

        Remark.objects.create(student=students["Aarav Iyer"], author=anita, body="Aarav showed excellent effort in the science project this week.", tone="positive")
        Remark.objects.create(student=students["Diya Iyer"], author=vikram, body="Diya read aloud confidently in class today. Keep it up!", tone="positive")
        Remark.objects.create(student=students["Kabir Sharma"], author=farah, body="Kabir should revise chemical equations before the next test.", tone="concern")

        now = timezone.now()
        announcements = [
            ("Parent–teacher meeting on Saturday, 27 September", "Meet your child's teachers between 9:00 AM and 1:00 PM. Book a slot with the class teacher.", Announcement.Kind.EVENT, Announcement.Audience.FAMILIES, True, 1),
            ("Annual Day rehearsal moves to the auditorium", "Rehearsals for Classes 6–10 are in the auditorium from 3:00 PM today.", Announcement.Kind.EVENT, Announcement.Audience.EVERYONE, False, 3),
            ("Route 4 pickup five minutes earlier from Monday", "From 22 September, Route 4 starts at 7:05 AM. Stop times move five minutes earlier.", Announcement.Kind.TRANSPORT, Announcement.Audience.FAMILIES, False, 20),
            ("Dussehra break: 2–5 October", "School is closed from Thursday 2 October to Sunday 5 October. Classes resume on Monday 6 October.", Announcement.Kind.HOLIDAY, Announcement.Audience.EVERYONE, False, 48),
        ]
        for title, body, kind, audience, ack, hours_ago in announcements:
            Announcement.objects.create(
                title=title, body=body, kind=kind, audience=audience, requires_ack=ack,
                created_by=principal, published_at=now - timedelta(hours=hours_ago),
            )

        self._seed_chat(school, anita, deepak, meera, students)
        self._seed_notifications(school, meera, students, today)
        _ = ravi  # transport manager receives safety alerts

    def _seed_transport(self, school, students, driver, attendant, today):
        vehicle = Vehicle.objects.create(
            registration_no="TS09 AB 4521", label="Bus 4", capacity=40, gps_device_id="358899051234567",
            has_gps_tracker=True, has_panic_button=True, has_cctv=True, has_speed_governor=True,
            fitness_valid_until=date(2027, 3, 31), permit_valid_until=today + timedelta(days=21),
            insurance_valid_until=date(2027, 1, 15), puc_valid_until=date(2026, 12, 1),
        )
        route = Route.objects.create(
            code="R4", name="Route 4 — North Campus", path=ROUTE_4_PATH, avg_speed_kmh=22,
            vehicle=vehicle, driver=driver, attendant=attendant,
            pickup_start=time(7, 10), drop_start=time(15, 40),
        )
        refresh_route_length(route)
        stops = {}
        for sequence, (name, lat, lng, pickup, drop, is_school) in enumerate(ROUTE_4_STOPS, start=1):
            stops[name] = Stop.objects.create(
                route=route, name=name, lat=lat, lng=lng, sequence=sequence,
                pickup_offset_min=pickup, drop_offset_min=drop, is_school=is_school,
            )
        riders = {
            "Aarav Iyer": "MG Road", "Diya Iyer": "MG Road", "Tanvi Rao": "MG Road",
            "Kabir Sharma": "Lake View", "Ira Deshpande": "Lake View",
            "Ananya Reddy": "Green Park", "Charvi Bose": "Green Park",
            "Rohan Verma": "Silver Oaks Colony", "Vivaan Mehta": "Silver Oaks Colony",
        }
        for name, stop_name in riders.items():
            StudentTransport.objects.create(
                student=students[name], route=route, pickup_stop=stops[stop_name], drop_stop=stops[stop_name]
            )
        ensure_trips_for_date(route, today)

    def _seed_attendance(self, groups, students, anita, vikram, farah, today):
        markers = {"8B": anita, "6A": vikram, "10C": farah}
        fixed = {"Aarav Iyer": {9: "absent", 4: "late"}, "Kabir Sharma": {12: "absent", 3: "absent"}}
        day, school_days = today - timedelta(days=1), 0
        while school_days < 24:
            if day.weekday() != 6:
                school_days += 1
                for key, group in groups.items():
                    session = AttendanceSession.objects.create(
                        class_group=group, date=day, marked_by=markers[key],
                        marked_at=datetime.combine(day, time(8, 40), tzinfo=school_tz(group.school)),
                    )
                    for student in Student.objects.filter(class_group=group):
                        status = fixed.get(student.full_name, {}).get(school_days)
                        if status is None and student.full_name not in fixed and student.full_name != "Diya Iyer":
                            roll = random.random()
                            status = "absent" if roll < 0.03 else ("late" if roll < 0.05 else None)
                        if status:
                            AttendanceException.objects.create(session=session, student=student, status=status)
            day -= timedelta(days=1)
        # Today: 8B is already marked (Aarav present); 6A and 10C are left for the teacher demo.
        AttendanceSession.objects.create(
            class_group=groups["8B"], date=today, marked_by=anita,
            marked_at=datetime.combine(today, time(8, 35), tzinfo=school_tz(groups["8B"].school)),
        )

    def _seed_homework(self, school, groups, subjects, students, anita, vikram, farah, meera, today):
        items = [
            ("8B", "MATH", anita, "Worksheet 4 — Linear equations", "Solve questions 1–20. Show every step.", -2, 2),
            ("8B", "SCI", anita, "Lab report — Reflection of light", "Write up Tuesday's mirror experiment with a diagram.", -1, 5),
            ("8B", "ENG", vikram, "Essay: My favourite festival", "300 words. Describe how your family celebrates.", -8, -4),
            ("6A", "ENG", vikram, "Chapter 5 questions", "Read chapter 5 and answer questions 1–5 in your notebook.", -1, 3),
            ("6A", "SCI", farah, "Draw the water cycle", "Label evaporation, condensation, precipitation and collection.", -5, -1),
            ("10C", "MATH", anita, "Quadratic equations practice set", "Exercise 4.3, questions 1–12.", -2, 4),
            ("10C", "SCI", farah, "Chemical reactions worksheet", "Balance the 15 equations on the sheet.", -3, 1),
        ]
        created = {}
        for key, code, teacher, title, description, assigned, due in items:
            created[title] = Homework.objects.create(
                class_group=groups[key], subject=subjects[code], title=title, description=description,
                assigned_by=teacher, assigned_on=today + timedelta(days=assigned), due_date=today + timedelta(days=due),
            )
        essay = created["Essay: My favourite festival"]
        submission = HomeworkSubmission.objects.create(
            homework=essay, student=students["Aarav Iyer"], submitted_by=meera,
            submitted_at=timezone.now() - timedelta(days=5), status=HomeworkSubmission.Status.REVIEWED,
            teacher_remark="Lovely description of Onam! Watch the spelling of 'celebrate'.",
        )
        photo = SubmissionPhoto(submission=submission, order=0)
        photo.image.save("essay-page-1.jpg", ContentFile(_notebook_page("My favourite festival - Onam")), save=False)
        photo.save()

    def _seed_fees(self, school, students, meera, rahul):
        plan = {
            "Aarav Iyer": [("Term 1 tuition", "tuition", 42000, date(2026, 6, 15), True), ("Term 2 tuition", "tuition", 42000, date(2026, 9, 30), False), ("Transport · Jul–Sep", "transport", 9500, date(2026, 9, 30), False)],
            "Diya Iyer": [("Term 1 tuition", "tuition", 38500, date(2026, 6, 15), True), ("Term 2 tuition", "tuition", 38500, date(2026, 9, 30), False), ("Transport · Jul–Sep", "transport", 9500, date(2026, 9, 30), False)],
            "Kabir Sharma": [("Term 1 tuition", "tuition", 44000, date(2026, 6, 15), True), ("Term 2 tuition", "tuition", 44000, date(2026, 9, 10), False), ("Transport · Jul–Sep", "transport", 9500, date(2026, 7, 10), True)],
        }
        payer = {"Aarav Iyer": meera, "Diya Iyer": meera, "Kabir Sharma": rahul}
        for name, invoices in plan.items():
            for title, category, amount, due, paid in invoices:
                invoice = FeeInvoice.objects.create(
                    student=students[name], title=title, category=category, amount=Decimal(amount), due_date=due,
                    paid_amount=Decimal(amount) if paid else Decimal(0),
                )
                if paid:
                    Payment.objects.create(
                        invoice=invoice, amount=invoice.amount, gateway="mock", status=Payment.Status.SUCCEEDED,
                        gateway_order_id="seed", gateway_payment_id="seed", paid_by=payer[name],
                        paid_at=datetime.combine(due - timedelta(days=5), time(19, 12), tzinfo=school_tz(school)),
                        receipt_no=next_receipt_no(school, due),
                    )

    def _seed_results(self, groups, subjects, students):
        exam_subjects = {
            "8B": ["MATH", "SCI", "ENG", "HIN", "SST"],
            "6A": ["MATH", "SCI", "ENG", "HIN", "SST"],
            "10C": ["MATH", "SCI", "ENG", "HIN", "SST", "CS"],
        }
        bias = {"Aarav Iyer": 84, "Diya Iyer": 89, "Kabir Sharma": 76}
        for key, group in groups.items():
            for exam_name, held_on, max_marks, bump in (("Unit Test 1", date(2026, 7, 18), 25, 0), ("Term 1", date(2026, 9, 10), 100, 3)):
                exam = Exam.objects.create(class_group=group, name=exam_name, held_on=held_on, is_published=True)
                for student in Student.objects.filter(class_group=group):
                    base = bias.get(student.full_name, random.randint(58, 90)) + bump
                    for code in exam_subjects[key]:
                        pct = max(30, min(99, base + random.randint(-9, 8)))
                        ExamMark.objects.create(
                            exam=exam, student=student, subject=subjects[code],
                            marks=Decimal(str(round(pct * max_marks / 100, 1))), max_marks=Decimal(max_marks),
                        )

    def _seed_chat(self, school, anita, deepak, meera, students):
        now = timezone.now()
        aarav, diya = students["Aarav Iyer"], students["Diya Iyer"]
        direct = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=aarav)
        ConversationMember.objects.create(conversation=direct, user=meera, side="family", label="Parent of Aarav")
        ConversationMember.objects.create(conversation=direct, user=anita, side="staff", label="Class teacher · Mathematics, Science")
        thread = [
            (anita, "Hello Mrs. Iyer, Aarav did very well in today's maths quiz. Please make sure he finishes Worksheet 4 by Monday.", timedelta(hours=20)),
            (meera, "Thank you, Ms. Rao! He'll finish it this weekend.", timedelta(hours=17)),
            (anita, "Wonderful, thank you.", timedelta(hours=2)),
        ]
        self._messages(direct, thread, now)
        ConversationMember.objects.filter(conversation=direct, user=meera).update(last_read_at=now - timedelta(hours=17))
        ConversationMember.objects.filter(conversation=direct, user=anita).update(last_read_at=now)

        office = Conversation.objects.create(kind=Conversation.Kind.DEPARTMENT, department=Department.ACCOUNTS, student=diya)
        ConversationMember.objects.create(conversation=office, user=meera, side="family", label="Parent of Diya")
        ConversationMember.objects.create(conversation=office, user=deepak, side="staff", label="Accounts")
        self._messages(
            office,
            [
                (meera, "Could you share the Term 2 fee breakup for Diya?", timedelta(days=2, hours=3)),
                (deepak, "Sure. Term 2 tuition is ₹38,500 and transport is ₹9,500. You can pay both in the app.", timedelta(days=2, hours=1)),
            ],
            now,
        )
        ConversationMember.objects.filter(conversation=office).update(last_read_at=now)

    def _messages(self, conversation, thread, now):
        last = None
        for index, (sender, body, ago) in enumerate(thread):
            message = Message.objects.create(conversation=conversation, sender=sender, body=body, client_id=f"seed-{conversation.id}-{index}")
            Message.objects.filter(pk=message.pk).update(created_at=now - ago)
            last = (now - ago, body)
        Conversation.objects.filter(pk=conversation.pk).update(last_message_at=last[0], last_message_preview=last[1][:140])

    def _seed_notifications(self, school, meera, students, today):
        now = timezone.now()
        items = [
            ("homework", "New homework: Worksheet 4 — Linear equations", "Mathematics · due Monday", 30),
            ("fees", "Term 2 fees due on 30 September", "Aarav and Diya · ₹99,500 in total. Pay securely in the app.", 26),
            ("announcement", "Parent–teacher meeting on Saturday, 27 September", "Meet your child's teachers between 9:00 AM and 1:00 PM.", 1),
            ("general", "A note about Aarav from Anita Rao", "Aarav showed excellent effort in the science project this week.", 5),
        ]
        for category, title, body, hours_ago in items:
            notification = Notification.objects.create(
                user=meera, school=school, category=category, title=title, body=body, push_status="skipped",
                dedupe_key=f"seed:{title}",
            )
            Notification.objects.filter(pk=notification.pk).update(created_at=now - timedelta(hours=hours_ago))

    # ------------------------------------------------------------------ Sunrise (second tenant)

    def _seed_sunrise(self, school):
        year = AcademicYear.objects.create(name="2026–27", starts_on=date(2026, 6, 15), ends_on=date(2027, 4, 30), is_current=True)
        maths = Subject.objects.create(name="Mathematics", code="MATH", color="#1F4E79")
        teacher = self._user("+919911100001", "Sneha Kulkarni", school, Role.TEACHER, title="Class Teacher · 7A")
        group = ClassGroup.objects.create(academic_year=year, grade="7", section="A", class_teacher=teacher)
        TeachingAssignment.objects.create(teacher=teacher, class_group=group, subject=maths)
        parent = self._user("+919911100002", "Amit Joshi", school, Role.PARENT, title="Parent")
        student = Student.objects.create(full_name="Ishita Joshi", admission_no="SP07A001", roll_no=1, class_group=group)
        StudentGuardian.objects.create(student=student, user=parent, relationship="father", is_primary=True)
        FeeInvoice.objects.create(student=student, title="Term 1 tuition", amount=Decimal(31000), due_date=date(2026, 10, 5))

    def _print_summary(self):
        self.stdout.write(self.style.SUCCESS("\nDemo data ready. School code: GHIS (second tenant: SPS)"))
        rows = [
            ("Parent (Aarav & Diya)", "+91 99000 00001"),
            ("Parent (Kabir)", "+91 99000 00002"),
            ("Student (Kabir, 10C)", "+91 99000 00003"),
            ("Class teacher 8B (Anita)", "+91 98000 00002"),
            ("Principal", "+91 98000 00001"),
            ("Transport manager", "+91 98000 00006"),
            ("Driver (Bus 4)", "+91 98000 00007"),
            ("Attendant (Bus 4)", "+91 98000 00008"),
            ("Accountant", "+91 98000 00005"),
            ("SPS parent (other school)", "+91 99111 00002"),
        ]
        for label, phone in rows:
            self.stdout.write(f"  {label:<28} {phone}")
        self.stdout.write("  Django admin: /admin  phone +919000000000  password eduflow-admin (dev only)")


def _notebook_page(title: str) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 1200), (253, 252, 248))
    draw = ImageDraw.Draw(image)
    for y in range(140, 1200, 44):
        draw.line([(60, y), (860, y)], fill=(200, 214, 230), width=2)
    draw.line([(110, 0), (110, 1200)], fill=(230, 160, 160), width=2)
    draw.text((130, 90), title, fill=(40, 40, 90))
    lines = [
        "Onam is my favourite festival. We celebrate it in",
        "Kerala with my grandparents every year.",
        "We make a pookalam with flowers in front of the",
        "house and eat a big sadya on a banana leaf.",
        "My favourite part is the boat race on television",
        "and the payasam my grandmother makes.",
    ]
    for index, line in enumerate(lines):
        draw.text((130, 150 + index * 44), line, fill=(40, 40, 90))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
