"""Console seed: academics (PAcademics, PTimetable, PExams).

- A clash-free live timetable: every teacher and room in one place per period (the base seed assigns teachers at
  random), with Economics periods for Grades 11–12 and two unstaffed gaps (11-B Economics, Grade 4 Music).
- Syllabus progress with the teachers' plan-to-date for every section and core subject, and this week's homework.
- A timetable draft for 6-B (a Thursday swap that double-books Nikhil Rao, and a Saturday change), cover taken
  earlier this week, and the revision the term timetable went live with.
- Unit Test 2 mark sheets in mixed states, class-teacher remarks, the half-yearly date sheet from the Monday two
  weeks out, and invigilators.

Idempotent under ``seed_design --only academics``.
"""

import random
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.academics.models import (
    ClassGroup,
    Student,
    Subject,
    SubjectVacancy,
    TeachingAssignment,
    TimetableDraft,
    TimetableRevision,
    TimetableSlot,
)
from apps.accounts.models import Membership, Role
from apps.core import tenant

POLICY = {"homework_limit": 12, "max_periods": 32, "section_capacity": 40}
SPECIAL_ROOMS = {"Lab 1", "Lab 2", "Art Room", "Music Room"}
SHARED_ROOMS = {"Ground"}
LOAD_CAP = 30

# Pairs whose teacher the designs name (kept as they are), on top of the base seed's fixed ones.
FIXED = {
    ("6-B", code): name
    for code, name in {
        "MATH": "Priya Menon", "SCI": "Joseph Thomas", "ENG": "Kavya Nair", "HIN": "Sunita Verma", "SST": "Ravi Kumar",
        "CS": "Nikhil Rao", "ART": "Farah Khan", "PE": "Vikram Singh", "MUS": "Farah Khan",
    }.items()
}
FIXED.update({
    ("6-A", "MATH"): "Priya Menon", ("7-A", "MATH"): "Priya Menon", ("7-C", "MATH"): "Priya Menon",
    ("6-C", "ENG"): "Kavya Nair", ("8-A", "SCI"): "Joseph Thomas", ("2-A", "ENG"): "Deepa Iyer", ("2-A", "MATH"): "Deepa Iyer",
    ("7-C", "CS"): "Nikhil Rao", ("12-A", "ECO"): "Meera Joshi", ("12-B", "ECO"): "Meera Joshi",
    ("10-A", "HIN"): "Sunita Verma", ("10-B", "HIN"): "Sunita Verma",
})

# Syllabus covered by grade (ENG, MATH, SCI, SST, HIN, CS); the plan to date is 46% everywhere.
SYLLABUS = {
    "1": (52, 55, 50, 48, 54, 60), "2": (50, 53, 49, 47, 51, 58), "3": (48, 51, 47, 45, 50, 55), "4": (47, 49, 44, 42, 46, 52),
    "5": (46, 48, 45, 41, 44, 50), "6": (49, 52, 46, 40, 45, 56), "7": (45, 47, 43, 38, 42, 51), "8": (44, 45, 38, 39, 43, 49),
    "9": (42, 34, 40, 37, 41, 47), "10": (48, 46, 44, 42, 45, 50), "11": (41, 43, 39, 40, None, 46), "12": (51, 55, 53, 44, None, 58),
}
SYLLABUS_CODES = ("ENG", "MATH", "SCI", "SST", "HIN", "CS")
PLAN = 46

# Homework tasks per section this week, by grade (the policy limit is 12).
HOMEWORK = {"1": 4, "2": 5, "3": 6, "4": 7, "5": 8, "6": 9, "7": 10, "8": 11, "9": 14, "10": 13, "11": 9, "12": 11}
HW_DESC = "Complete in your notebook and bring it to class."
HW_TITLES = {
    "ENG": ["Reading log", "Grammar worksheet", "Comprehension passage", "Vocabulary list", "Paragraph writing"],
    "MATH": ["Practice set", "Worksheet · word problems", "Revision exercise", "Mental maths drill", "Chapter exercise"],
    "SCI": ["Diagram practice", "Chapter questions", "Lab observation", "Revision notes", "Activity sheet"],
    "SST": ["Map practice", "Chapter questions", "Timeline", "Revision notes", "Source reading"],
    "HIN": ["पाठ के प्रश्न", "व्याकरण अभ्यास", "सुलेख", "शब्दार्थ", "निबंध"],
    "CS": ["Typing practice", "Flowchart", "Worksheet", "Quiz prep", "Scratch task"],
    "ECO": ["Case study", "Chapter questions", "Data table", "Revision notes", "Graph practice"],
}

# UT2 mark sheets for Grades 6–10: the state of each grade × subject.
UT2_STATES = {
    "6": {c: "published" for c in ("MATH", "SCI", "ENG", "SST", "HIN", "CS")},
    "7": {"MATH": "published", "SCI": "published", "ENG": "published", "SST": "published", "HIN": "published", "CS": ("review", "Moderation check")},
    "8": {"MATH": "published", "SCI": "published", "ENG": "published", "SST": "submitted", "HIN": "published", "CS": "published"},
    "9": {"MATH": ("review", "Down 6 pts · flagged"), "SCI": "submitted", "ENG": "published", "SST": "submitted", "HIN": ("progress", 58), "CS": "published"},
    "10": {"MATH": "submitted", "SCI": ("progress", 41), "ENG": "submitted", "SST": "submitted", "HIN": ("progress", 0), "CS": ("progress", 60)},
}
REMARKED = {"7-A", "7-B", "7-C", "8-A", "8-B", "8-C", "9-A"}


@contextmanager
def _this_school_only():
    """``seed_design --only`` runs inside ``unscoped()``; switch the bypass off so every query here stays in this school."""
    token = tenant._bypass_var.set(False)
    try:
        yield
    finally:
        tenant._bypass_var.reset(token)


def seed(cmd, school):
    with tenant.use_school(school), _this_school_only():
        _seed(cmd, school)


def _seed(cmd, school):
    rng = random.Random(2609)
    school.settings = {**(school.settings or {}), "academics": {**POLICY, **((school.settings or {}).get("academics") or {})}}
    school.save(update_fields=["settings"])
    ctx = _Context(cmd, school)
    _streams_and_music(ctx)
    _repair_timetable(ctx)
    _vacancies(ctx)
    _syllabus(ctx, rng)
    _homework(ctx, rng)
    _timetable_draft(ctx)
    _exams(ctx, rng)


class _Context:
    def __init__(self, cmd, school):
        self.cmd = cmd
        self.school = school
        self.today = cmd.today
        self.groups = {g.short_label: g for g in ClassGroup.objects.select_related("class_teacher")}
        self.subjects = {s.code: s for s in Subject.objects.all()}
        self.teachers = {m.user_id: m.user for m in Membership.objects.filter(role=Role.TEACHER, is_active=True).select_related("user")}
        self.named = {u.full_name: u for u in self.teachers.values()}
        self.principal = cmd.principal


# ------------------------------------------------------------------ timetable structure


def _restore_6b(ctx):
    """6-B's week is the one the family apps are designed around; put it back if the console changed it."""
    from apps.core.management.commands.seed_design import WEEK_6B

    g = ctx.groups["6-B"]
    rooms = {"SCI": "Lab 1", "CS": "Lab 2", "ART": "Art Room", "PE": "Ground", "MUS": "Music Room"}
    for s in TimetableSlot.objects.filter(class_group=g):
        day = WEEK_6B[s.weekday] if s.weekday < len(WEEK_6B) else []
        if s.period > len(day):
            continue
        code = day[s.period - 1]
        teacher = ctx.named[FIXED[("6-B", code)]]
        if (s.subject.code, s.teacher_id, s.room) != (code, teacher.id, rooms.get(code, "Room 204")):
            s.subject, s.teacher, s.room = ctx.subjects[code], teacher, rooms.get(code, "Room 204")
            s.save(update_fields=["subject", "teacher", "room"])


def _streams_and_music(ctx):
    """Grades 11–12 take Economics instead of Hindi (6 periods); Grade 4 gets two Music periods a week."""
    _restore_6b(ctx)
    eco = ctx.subjects["ECO"]
    for key in ("11-A", "11-B", "12-A", "12-B"):
        g = ctx.groups[key]
        slots = list(TimetableSlot.objects.filter(class_group=g, subject__code__in=["HIN", "SST"]).order_by("subject__code", "weekday", "period"))
        have = TimetableSlot.objects.filter(class_group=g, subject=eco).count()
        hin = [s for s in slots if s.subject.code == "HIN"]
        sst = [s for s in slots if s.subject.code == "SST"]
        for s in (hin + sst)[: max(0, 6 - have)]:
            s.subject = eco
            s.save(update_fields=["subject"])
        TimetableSlot.objects.filter(class_group=g, subject__code="HIN").update(subject=ctx.subjects["SST"])
        TeachingAssignment.objects.filter(class_group=g, subject__code="HIN").delete()
        if not TeachingAssignment.objects.filter(class_group=g, subject=eco).exists():
            TeachingAssignment.objects.create(class_group=g, subject=eco, teacher=ctx.named["Meera Joshi"])

    mus = ctx.subjects["MUS"]
    taken = set()
    for key in ("4-A", "4-B", "4-C"):
        g = ctx.groups[key]
        have = list(TimetableSlot.objects.filter(class_group=g, subject=mus))
        taken |= {(s.weekday, s.period) for s in have}
        counts = Counter(TimetableSlot.objects.filter(class_group=g).values_list("subject__code", flat=True))
        for s in TimetableSlot.objects.filter(class_group=g, subject__code__in=["ART", "PE"], weekday__lt=5).order_by("weekday", "period"):
            if len(have) >= 2:
                break
            if (s.weekday, s.period) in taken or counts[s.subject.code] < 3:
                continue
            counts[s.subject.code] -= 1
            s.subject, s.teacher, s.room = mus, None, "Music Room"
            s.save(update_fields=["subject", "teacher", "room"])
            have.append(s)
            taken.add((s.weekday, s.period))

    # 7-C has Computer in Thursday P5 (Nikhil Rao), which 6-B's draft swap runs into.
    g7c = ctx.groups["7-C"]
    p5 = TimetableSlot.objects.get(class_group=g7c, weekday=3, period=5)
    if p5.subject.code != "CS":
        cs = TimetableSlot.objects.filter(class_group=g7c, weekday=3, subject__code="CS").first()
        if cs is not None:
            cs.subject, p5.subject = p5.subject, cs.subject
            cs.teacher, p5.teacher = p5.teacher, cs.teacher
            cs.room, p5.room = p5.room, cs.room
            cs.save(update_fields=["subject", "teacher", "room"])
            p5.save(update_fields=["subject", "teacher", "room"])
        elif p5.subject.code != "MATH":
            p5.subject, p5.room = ctx.subjects["CS"], "Lab 2"
            p5.save(update_fields=["subject", "room"])


def _repair_timetable(ctx):
    """Give every section × subject a teacher who is free at all its periods and under the load cap,
    keeping the designs' named pairs and anyone already free. Then clear room double-bookings."""
    groups, subjects, named = ctx.groups, ctx.subjects, ctx.named
    by_id = {g.id: g for g in groups.values()}
    pairs = defaultdict(list)
    for s in TimetableSlot.objects.all():
        pairs[(s.class_group_id, s.subject_id)].append(s)
    current = {(a.class_group_id, a.subject_id): a for a in TeachingAssignment.objects.all()}
    pool = defaultdict(set)
    for (gid, sid), a in current.items():
        pool[sid].add(a.teacher_id)
    for (key, code), name in FIXED.items():
        pool[subjects[code].id].add(named[name].id)

    fixed = {(groups[key].id, subjects[code].id): named[name].id for (key, code), name in FIXED.items() if key in groups}
    # The gaps are held for the teachers the page will suggest, so the suggestion stays free.
    fixed[(groups["11-B"].id, subjects["ECO"].id)] = named["Meera Joshi"].id
    for key in ("4-A", "4-B", "4-C"):
        fixed[(groups[key].id, subjects["MUS"].id)] = named["Farah Khan"].id
    # Keep Sunita Verma free on Thursday P6, where 6-B's draft moves her Hindi.
    blocked = {named["Sunita Verma"].id: {(3, 6)}}

    # Two of a named teacher's classes can meet at once in the base timetable: move the other class's period
    # to a time that teacher is free (swapping with a period of the same day), before anyone else is placed.
    protected = {groups[k].id for k in ("6-B", "6-A", "7-A", "7-C") if k in groups}
    held = defaultdict(set)
    for pair, tid in sorted(fixed.items(), key=lambda kv: (kv[0][0] not in protected, str(kv[0]))):
        for s in sorted(pairs.get(pair, []), key=lambda s: (s.weekday, s.period)):
            if (s.weekday, s.period) not in held[tid]:
                held[tid].add((s.weekday, s.period))
                continue
            day = [o for o in TimetableSlot.objects.filter(class_group_id=s.class_group_id, weekday=s.weekday) if o.id != s.id]
            for o in day:
                other = fixed.get((o.class_group_id, o.subject_id))
                if (s.weekday, o.period) in held[tid] or (other and (s.weekday, s.period) in held[other]):
                    continue
                o.subject_id, s.subject_id = s.subject_id, o.subject_id
                o.room, s.room = s.room, o.room
                o.teacher_id, s.teacher_id = s.teacher_id, o.teacher_id
                o.save(update_fields=["subject", "room", "teacher"])
                s.save(update_fields=["subject", "room", "teacher"])
                held[tid].add((s.weekday, o.period))
                if other:
                    held[other].discard((s.weekday, o.period))
                    held[other].add((s.weekday, s.period))
                break
    pairs = defaultdict(list)
    for s in TimetableSlot.objects.all():
        pairs[(s.class_group_id, s.subject_id)].append(s)

    busy = defaultdict(set)
    load = Counter()
    choice = {}
    for pair, tid in fixed.items():
        if pair not in pairs:
            continue
        times = {(s.weekday, s.period) for s in pairs[pair]}
        busy[tid] |= times
        load[tid] += len(pairs[pair])
        choice[pair] = tid

    def fits(tid, times, n, cap=LOAD_CAP):
        return not (busy[tid] & times) and not (blocked.get(tid, set()) & times) and load[tid] + n <= cap and tid in ctx.teachers

    rest = sorted((p for p in pairs if p not in choice), key=lambda p: (-len(pairs[p]), str(p)))
    for pair in rest:
        times = {(s.weekday, s.period) for s in pairs[pair]}
        n = len(pairs[pair])
        keep = current[pair].teacher_id if pair in current else None
        if fits(keep, times, n):
            tid = keep
        else:
            options = sorted(pool[pair[1]], key=lambda t: (load[t], str(t)))
            tid = next((t for t in options if fits(t, times, n)), None)
            if tid is None:
                tid = next((t for t in sorted(ctx.teachers, key=lambda t: (load[t], str(t))) if fits(t, times, n, 34)), None)
        if tid is None:
            free = [t for t in ctx.teachers if not (busy[t] & times)]
            ctx.cmd.stdout.write(f"  academics: no free teacher for {by_id[pair[0]].short_label} {pair[1]} ({n} periods; {len(free)} free, loads {sorted(load[t] for t in free)[:5]})")
            continue
        busy[tid] |= times
        load[tid] += n
        choice[pair] = tid

    for pair, tid in choice.items():
        a = current.get(pair)
        if a is not None and a.teacher_id != tid:
            if TeachingAssignment.objects.filter(teacher_id=tid, class_group_id=pair[0], subject_id=pair[1]).exists():
                a.delete()
            else:
                a.teacher_id = tid
                a.save(update_fields=["teacher"])
        elif a is None:
            TeachingAssignment.objects.get_or_create(teacher_id=tid, class_group_id=pair[0], subject_id=pair[1])
        TimetableSlot.objects.filter(class_group_id=pair[0], subject_id=pair[1]).exclude(teacher_id=tid).update(teacher_id=tid)

    # Rooms: one class per lab, art or music room at a time; the others use their own classroom.
    home = {}
    for g in groups.values():
        rooms = Counter(r for r in TimetableSlot.objects.filter(class_group=g).values_list("room", flat=True) if r.startswith("Room"))
        home[g.id] = rooms.most_common(1)[0][0] if rooms else f"Room {g.grade}{g.section}"
    order = {g.id: i for i, g in enumerate(sorted(groups.values(), key=lambda g: (g.short_label != "6-B", g.short_label != "7-C", g.grade.zfill(3), g.section)))}
    # The labs are for the middle and senior school; younger classes have science and computers in their room.
    junior = [g.id for g in groups.values() if not (g.grade.isdigit() and int(g.grade) >= 6)]
    for s in TimetableSlot.objects.filter(room__in=["Lab 1", "Lab 2"], class_group_id__in=junior):
        s.room = home[s.class_group_id]
        s.save(update_fields=["room"])
    seen = defaultdict(list)
    for s in TimetableSlot.objects.filter(room__in=SPECIAL_ROOMS):
        seen[(s.weekday, s.period, s.room)].append(s)
    for slots in seen.values():
        slots.sort(key=lambda s: order[s.class_group_id])
        for s in slots[1:]:
            s.room = home[s.class_group_id]
            s.save(update_fields=["room"])


def _vacancies(ctx):
    """11-B Economics has been vacant for 12 days; Grade 4 Music lost its teacher to long leave."""
    today = ctx.today
    gaps = [(ctx.groups["11-B"], ctx.subjects["ECO"], today - timedelta(days=12), "vacant")] + [
        (ctx.groups[k], ctx.subjects["MUS"], today - timedelta(days=30), "long_leave") for k in ("4-A", "4-B", "4-C")
    ]
    for g, subject, since, reason in gaps:
        TeachingAssignment.objects.filter(class_group=g, subject=subject).delete()
        TimetableSlot.objects.filter(class_group=g, subject=subject).update(teacher=None)
        SubjectVacancy.objects.filter(class_group=g, subject=subject).delete()
        SubjectVacancy.objects.create(class_group=g, subject=subject, since=since, reason=reason)


# ------------------------------------------------------------------ syllabus and homework


def _syllabus(ctx, rng):
    from apps.learning.models import SyllabusProgress

    teacher = {(a.class_group_id, a.subject_id): a.teacher for a in TeachingAssignment.objects.select_related("teacher")}
    taught = set(TimetableSlot.objects.values_list("class_group_id", "subject_id"))
    for g in ctx.groups.values():
        if g.grade not in SYLLABUS:
            continue
        for code, base in zip(SYLLABUS_CODES, SYLLABUS[g.grade]):
            subject = ctx.subjects[code]
            if base is None or (g.id, subject.id) not in taught:
                SyllabusProgress.objects.filter(class_group=g, subject=subject).delete()
                continue
            existing = SyllabusProgress.objects.filter(class_group=g, subject=subject).first()
            # Rows the teacher apps already show (6-B, Priya's sections) keep their own numbers.
            if existing and (g.short_label == "6-B" or (code == "MATH" and g.short_label in ("6-A", "7-A", "7-C"))):
                existing.planned_percent = PLAN
                existing.save(update_fields=["planned_percent"])
                continue
            spread = {"A": -2, "B": 2, "C": 0}.get(g.section, 0) if g.grade != "9" or code != "MATH" else {"A": -2, "B": 2}.get(g.section, 0)
            SyllabusProgress.objects.update_or_create(
                class_group=g, subject=subject,
                defaults={"percent": max(0, base + spread), "planned_percent": PLAN, "updated_by": teacher.get((g.id, subject.id))},
            )


def _homework(ctx, rng):
    from apps.homework.models import Homework

    today = ctx.today
    monday = today - timedelta(days=today.weekday())
    days = [monday + timedelta(days=i) for i in range(6) if monday + timedelta(days=i) <= today]
    Homework.objects.filter(description=HW_DESC, assigned_on__gte=monday - timedelta(days=14)).delete()
    teacher = {(a.class_group_id, a.subject.code): a.teacher for a in TeachingAssignment.objects.select_related("teacher", "subject")}
    priya = ctx.named["Priya Menon"]
    thursday = monday + timedelta(days=3)
    by_grade = defaultdict(list)
    for g in ctx.groups.values():
        by_grade[g.grade].append(g)
    for grade, target in HOMEWORK.items():
        sections = sorted(by_grade.get(grade, []), key=lambda g: g.section)
        have = Counter(Homework.objects.filter(class_group__in=sections, assigned_on__gte=monday, assigned_on__lte=monday + timedelta(days=5)).values_list("class_group_id", flat=True))
        # 6-B's own diary is what the family apps show, so the rest of Grade 6 carries its share.
        protected = [g for g in sections if g.short_label == "6-B"]
        open_ = [g for g in sections if g not in protected]
        deficit = target * len(sections) - sum(have.values())
        per = {g.id: max(0, target - have.get(g.id, 0)) for g in open_}
        for i in range(max(0, deficit - sum(per.values()))):
            per[open_[i % len(open_)].id] += 1
        for g in open_:
            codes = [c for c in ("ENG", "MATH", "SCI", "SST", "HIN", "CS", "ECO") if (g.id, c) in teacher and not (c == "MATH" and teacher[(g.id, c)] == priya)]
            for i in range(per[g.id]):
                code = codes[i % len(codes)]
                assigned = days[i % len(days)]
                # Grade 9: five of the week's tasks all fall due on Thursday.
                if grade == "9" and i < 5:
                    assigned = min(days[0], thursday - timedelta(days=1)) if days else monday
                    due = thursday
                else:
                    due = assigned + timedelta(days=2 + i % 3)
                    if due.weekday() == 6:
                        due += timedelta(days=1)
                Homework.objects.create(
                    class_group=g, subject=ctx.subjects[code], title=HW_TITLES[code][i % 5], description=HW_DESC,
                    assigned_by=teacher[(g.id, code)], assigned_on=assigned, due_date=due, accepts_photos=True,
                )


# ------------------------------------------------------------------ timetable draft and cover


def _timetable_draft(ctx):
    from apps.staff.models import Substitution

    today, named = ctx.today, ctx.named
    TimetableRevision.objects.all().delete()
    TimetableRevision.objects.create(published_by=ctx.principal, published_at=timezone.now() - timedelta(days=82), changes=0, summary="Term 1 timetable")

    g6b = ctx.groups["6-B"]
    TimetableDraft.objects.filter(class_group=g6b).delete()
    thu = {s.period: s for s in TimetableSlot.objects.filter(class_group=g6b, weekday=3)}
    swap = uuid.uuid4()
    # Thursday: Computer (P6) and Hindi (P5) change places.
    a, b = thu[5], thu[6]
    TimetableDraft.objects.create(class_group=g6b, weekday=3, period=5, subject=b.subject, teacher=b.teacher, room=b.room, batch=swap, kind="swap", created_by=ctx.principal)
    TimetableDraft.objects.create(class_group=g6b, weekday=3, period=6, subject=a.subject, teacher=a.teacher, room=a.room, batch=swap, kind="swap", created_by=ctx.principal)
    # Saturday: the last period becomes a Maths remedial with Priya Menon.
    sat = TimetableSlot.objects.filter(class_group=g6b, weekday=5).order_by("-period").first()
    TimetableDraft.objects.create(
        class_group=g6b, weekday=5, period=sat.period, subject=ctx.subjects["MATH"], teacher=named["Priya Menon"], room="Room 204",
        batch=uuid.uuid4(), kind="change", created_by=ctx.principal,
    )

    # Cover earlier this week in 6-B: Kavya Nair and Sunita Verma were at a board workshop on Tuesday.
    tuesday = today - timedelta(days=(today.weekday() - 1) % 7 or 7)
    for period, why in ((2, "board workshop"), (4, "board workshop")):
        slot = TimetableSlot.objects.filter(class_group=g6b, weekday=1, period=period).select_related("teacher", "subject").first()
        if slot is None or slot.teacher is None:
            continue
        busy = set(TimetableSlot.objects.filter(weekday=1, period=period).exclude(teacher=None).values_list("teacher_id", flat=True))
        busy |= set(Substitution.objects.filter(date=tuesday, slot__period=period).exclude(slot=slot).values_list("teacher_id", flat=True))
        prefer = [named.get("Arjun Das"), named.get("Rekha Kulkarni"), named.get("Sameer Naik")]
        cover = next((t for t in prefer if t and t.id not in busy), None) or next(t for t in ctx.teachers.values() if t.id not in busy)
        Substitution.objects.update_or_create(
            date=tuesday, slot=slot,
            defaults={"teacher": cover, "absent_teacher": slot.teacher, "reason": why, "assigned_by": ctx.principal, "assigned_at": timezone.now() - timedelta(days=(today - tuesday).days, hours=2)},
        )


# ------------------------------------------------------------------ exams


def _exams(ctx, rng):
    from apps.announcements.models import Announcement
    from apps.results.models import Exam, ExamMark, ExamPaper, ExamSeries, Invigilation, MarkCorrection, MarkSheet, ReportCardNote

    today, subjects, groups = ctx.today, ctx.subjects, ctx.groups
    now = timezone.now()
    tz = ctx.cmd.tz

    def at(day, h=16, m=10):
        return datetime.combine(day, time(h, m), tzinfo=tz)

    ExamSeries.objects.update_or_create(
        name="Unit Test 1",
        defaults={"weightage": 10, "grading_locked_at": now - timedelta(days=80), "template_approved_at": now - timedelta(days=75), "template_approved_by": ctx.principal,
                  "published_at": now - timedelta(days=60), "published_by": ctx.principal, "parents_notified_at": now - timedelta(days=60)},
    )
    ExamSeries.objects.update_or_create(
        name="Unit Test 2",
        defaults={"weightage": 10, "grading_locked_at": now - timedelta(days=20), "grading_locked_by": ctx.principal,
                  "template_approved_at": at(today - timedelta(days=12), 11, 20), "template_approved_by": ctx.principal,
                  "published_at": None, "published_by": None, "parents_notified_at": None},
    )
    ExamSeries.objects.update_or_create(name="Half-yearly", defaults={"weightage": 30, "invigilators_per_room": 2})

    # Moderation decisions made on the console for corrections still waiting are cleared.
    for corr in MarkCorrection.objects.filter(applied_at=None):
        if any("decision" in e for e in corr.entries):
            corr.entries = [{k: v for k, v in e.items() if k != "decision"} for e in corr.entries]
            corr.save(update_fields=["entries"])

    # --- Grade 9 Maths fell in Unit Test 2: 9-A 71 → 63, 9-B 77 → 73 -------------------------------
    math = subjects["MATH"]
    for key, targets in {"9-A": (71, 63), "9-B": (77, 73)}.items():
        for name, target in zip(("Unit Test 1", "Unit Test 2"), targets):
            rows = list(ExamMark.objects.filter(exam__class_group=groups[key], exam__name=name, subject=math, is_absent=False))
            for _ in range(4):
                if not rows:
                    break
                avg = sum(float(r.marks) * 100 / float(r.max_marks) for r in rows) / len(rows)
                for r in rows:
                    r.marks = Decimal(max(15, min(100, round(float(r.marks) + target - avg))))
            ExamMark.objects.bulk_update(rows, ["marks"])

    # --- Unit Test 2 mark sheets, Grades 6–10 ---------------------------------------------------------
    held = Exam.objects.filter(name="Unit Test 2").order_by("held_on").values_list("held_on", flat=True).first() or today - timedelta(days=4)
    published_on = {"6": 1, "7": 2, "8": 2, "9": 3, "10": 3}
    priya_sheet = MarkSheet.objects.filter(exam__class_group=groups["7-C"], exam__name="Unit Test 2", subject=math).first()
    for grade, states in UT2_STATES.items():
        for g in sorted((g for g in groups.values() if g.grade == grade), key=lambda g: g.section):
            exam = Exam.objects.get(class_group=g, name="Unit Test 2")
            roster = list(Student.objects.filter(class_group=g, is_active=True).order_by("roll_no"))
            any_published = False
            for code, state in states.items():
                subject = subjects[code]
                if priya_sheet and exam.id == priya_sheet.exam_id and code == "MATH":
                    continue  # Priya is still entering 7-C Maths in the staff app.
                kind, extra = (state, None) if isinstance(state, str) else state
                teacher = TeachingAssignment.objects.filter(class_group=g, subject=subject).select_related("teacher").first()
                who = teacher.teacher if teacher else None
                day = held + timedelta(days=published_on[grade])
                sheet, _ = MarkSheet.objects.update_or_create(
                    exam=exam, subject=subject,
                    defaults={
                        "max_marks": Decimal(100), "due_on": held + timedelta(days=7),
                        "status": {"published": "published", "submitted": "submitted", "review": "review", "progress": "open"}[kind],
                        "saved_at": at(day, 15, 30), "saved_by": who,
                        "submitted_at": at(day, 16, 10) if kind in ("published", "submitted", "review") else None,
                        "submitted_by": who if kind in ("published", "submitted", "review") else None,
                        "review_note": extra if kind == "review" else "",
                        "published_at": at(day, 18, 0) if kind == "published" else None,
                        "published_by": ctx.principal if kind == "published" else None,
                        "moderated_at": None, "moderated_by": None,
                    },
                )
                any_published |= kind == "published"
                # Marks: complete except for sheets still being entered (the grade's total is split over sections).
                have = {m.student_id: m for m in ExamMark.objects.filter(exam=exam, subject=subject)}
                missing = [s for s in roster if s.id not in have]
                for s in missing:
                    ExamMark.objects.create(exam=exam, student=s, subject=subject, marks=Decimal(max(30, min(98, round(rng.gauss(72, 10))))), max_marks=Decimal(100))
                if kind == "progress":
                    sections = [x for x in groups.values() if x.grade == grade]
                    total = sum(Student.objects.filter(class_group=x, is_active=True).count() for x in sections)
                    share = round(extra * len(roster) / total) if total else 0
                    keep = {s.id for s in roster[:share]}
                    ExamMark.objects.filter(exam=exam, subject=subject).exclude(student_id__in=keep).delete()
                    if extra == 0:
                        MarkSheet.objects.filter(pk=sheet.pk).update(saved_at=None, saved_by=None)
            # Nothing of Grade 10's Unit Test 2 is out yet; the other grades are part-way.
            if not any_published and exam.is_published:
                Exam.objects.filter(pk=exam.pk).update(is_published=False, published_on=None)

    # --- Class-teacher remarks on the UT2 report card: 7 of the 10 sections in Grades 7–10 ------------------
    notes = []
    for key in ("7-A", "7-B", "7-C", "8-A", "8-B", "8-C", "9-A", "9-B", "10-A", "10-B"):
        g = groups[key]
        exam = Exam.objects.get(class_group=g, name="Unit Test 2")
        if key not in REMARKED:
            ReportCardNote.objects.filter(exam=exam).delete()
            continue
        done = set(ReportCardNote.objects.filter(exam=exam).values_list("student_id", flat=True))
        for s in Student.objects.filter(class_group=g, is_active=True).exclude(id__in=done):
            notes.append(ReportCardNote(school=ctx.school, exam=exam, student=s, author=g.class_teacher, body=rng.choice(REMARKS)))
    ReportCardNote.objects.bulk_create(notes, batch_size=1000)

    # --- Half-yearly date sheet: from the Monday at least two weeks out, 9:00 AM ---------------------------
    first = today + timedelta(days=14)
    first += timedelta(days=(7 - first.weekday()) % 7)

    def day(n):
        return ctx.cmd._school_day(first, n) if n else first

    middle = [g for g in groups.values() if g.grade in ("6", "7", "8", "9", "10")]
    senior = [g for g in groups.values() if g.grade in ("11", "12")]
    plan = [
        (0, middle, "ENG", time(9), time(12)), (0, senior, "ENG", time(9), time(12, 15)),
        (2, middle + senior, "MATH", time(9), time(12)),
        (4, middle, "SCI", time(9), time(12)), (4, senior, "ECO", time(9), time(12)),
        (5, middle, "HIN", time(9), time(11, 30)),
        (6, middle, "SST", time(9), time(12)),
        (8, middle, "CS", time(9), time(11)),
        (9, senior, "CS", time(9), time(12)),
        (10, senior, "SCI", time(9), time(12)),
    ]
    home = {}
    for g in groups.values():
        rooms = Counter(r for r in TimetableSlot.objects.filter(class_group=g).values_list("room", flat=True) if r.startswith("Room"))
        home[g.id] = rooms.most_common(1)[0][0] if rooms else ""
    papers = []
    keep_ids = set()
    for offset, sections, code, starts, ends in plan:
        for g in sections:
            exam, _ = Exam.objects.get_or_create(class_group=g, name="Half-yearly", defaults={"held_on": first, "results_on": first + timedelta(days=18)})
            paper = ExamPaper.objects.filter(exam=exam, subject=subjects[code]).first() or ExamPaper(exam=exam, subject=subjects[code])
            paper.date, paper.starts_at, paper.ends_at, paper.room = day(offset), starts, ends, home[g.id]
            paper.save()
            keep_ids.add(paper.id)
            papers.append(paper)
    ExamPaper.objects.filter(exam__name="Half-yearly", exam__class_group__grade__in=["6", "7", "8", "9", "10", "11", "12"]).exclude(id__in=keep_ids).delete()
    for exam in Exam.objects.filter(name="Half-yearly"):
        exam.held_on = first
        exam.admit_cards_from = ctx.cmd._school_day(first, -6)
        exam.report_by = time(8, 45)
        exam.results_on = first + timedelta(days=18)
        exam.save(update_fields=["held_on", "admit_cards_from", "report_by", "results_on", "updated_at"])

    # --- Invigilators: two per room. The first papers are fully staffed, later ones part-way or not yet --------
    Invigilation.objects.filter(paper__exam__name="Half-yearly").delete()
    fill = {(0, "ENG"): 1.0, (2, "MATH"): 0.95, (4, "SCI"): 1.0, (4, "ECO"): 0.75, (5, "HIN"): 0.54}
    priya = ctx.named["Priya Menon"]
    teachers = sorted(ctx.teachers.values(), key=lambda u: u.full_name)
    duty = Counter()
    busy = defaultdict(set)
    g6b = groups["6-B"]
    priya_papers = sorted((p for p in papers if p.exam.class_group_id == g6b.id), key=lambda p: p.date)[:3]
    rows = []
    for p in priya_papers:
        rows.append(Invigilation(school=ctx.school, paper=p, teacher=priya, assigned_by=ctx.principal))
        busy[(p.date, p.starts_at)].add(priya.id)
        duty[priya.id] += 1
    by_row = defaultdict(list)
    for offset, sections, code, _s, _e in plan:
        for p in papers:
            if p.date == day(offset) and p.subject.code == code and p.exam.class_group in sections:
                by_row[(offset, code)].append(p)
    for (offset, code), row_papers in by_row.items():
        share = fill.get((offset, code), 0)
        need = round(share * 2 * len(row_papers))
        slots = [(p, i) for i in range(2) for p in sorted(row_papers, key=lambda p: p.exam.class_group.short_label)]
        placed = sum(1 for r in rows if r.paper in row_papers)
        for p, _i in slots:
            if placed >= need:
                break
            if sum(1 for r in rows if r.paper is p) >= 2:
                continue
            key = (p.date, p.starts_at)
            t = min((t for t in teachers if t.id not in busy[key] and t.id != priya.id), key=lambda t: (duty[t.id], t.full_name), default=None)
            if t is None:
                break
            rows.append(Invigilation(school=ctx.school, paper=p, teacher=t, assigned_by=ctx.principal))
            busy[key].add(t.id)
            duty[t.id] += 1
            placed += 1
    Invigilation.objects.bulk_create(rows)

    # Priya's roster notice (StaffHome) follows the new dates.
    if priya_papers:
        confirm = ctx.cmd._school_day(today, 2)
        dates = [p.date for p in priya_papers]
        Announcement.objects.filter(title="Half-yearly invigilation roster").update(
            body=(
                f"Half-yearly exams begin {first:%a} {first.day} {first:%b}. Your duties: "
                f"{', '.join(str(d.day) for d in dates[:-1])} and {dates[-1].day} {dates[-1]:%b}, {priya_papers[0].room}, 9:00 AM. "
                f"Please confirm by {confirm:%a} {confirm.day} {confirm:%b}."
            )
        )


REMARKS = [
    "Steady effort this term. Keep revising a little every day before the half-yearly.",
    "Participates well in class. Needs to show working in every answer.",
    "Good progress since Unit Test 1. Reading more will help with long answers.",
    "Attentive and curious. Practise the diagrams and map work before the exams.",
    "Capable of more. Complete homework on time and ask for help early.",
    "Consistent and well organised. Keep up the careful revision.",
]
