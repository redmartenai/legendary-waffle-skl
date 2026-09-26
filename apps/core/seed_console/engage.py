"""Console seed: engage (Communication, Documents, Reports & Analytics).

Idempotent under ``seed_design --only engage``: everything it makes is removed first. Its users live in
``cmd._phone(6000..6999)``: guardians (6000–6989) for students who don't have one yet, so reach and read
rates have real parents behind them, and the parents who asked the principal for a meeting (699x).
"""

import random
from datetime import datetime, time, timedelta

from django.core.files.base import ContentFile
from django.utils import timezone

from apps.academics.models import ClassGroup, Student, StudentGuardian
from apps.accounts.models import Membership, PushDevice, Role, User
from apps.announcements.models import Announcement, AnnouncementAck, AnnouncementRead, ChannelDelivery, SmsCreditTopUp
from apps.messaging.models import Conversation, ConversationMember, Meeting, Message
from apps.notifications.models import Notification

GUARDIANS = range(6000, 6990)
ADULT_FIRST = [
    "Rajesh", "Sunita", "Anil", "Kavita", "Suresh", "Pooja", "Vikas", "Neha", "Manoj", "Sneha", "Arvind", "Deepa",
    "Sanjay", "Anjali", "Rakesh", "Swati", "Ajay", "Priti", "Mahesh", "Rekha", "Nitin", "Shweta", "Prakash", "Lata",
    "Girish", "Meenakshi", "Harish", "Vandana", "Kiran", "Shalini", "Ashok", "Uma", "Venkat", "Radha", "Imtiaz", "Farida",
]
# Titles of what this module creates, so a re-run can find and replace them.
MY_ANNOUNCEMENTS = (
    "Unit Test 2 results published",
    "School closed Fri 2 Oct · Gandhi Jayanti",
    "Science fair registrations open",
    "Half-yearly exam schedule",
    "Junior dismissal at 1:45 PM",
    "Annual day costume guidelines",
    "Bus pass renewal for Term 2",
)
DRAFT_TITLE = "Half-yearly exam timetable — Grades 6–8"
DRAFT_BODY = """Dear parents,

The half-yearly examinations for Grades 6–8 begin on Monday, 12 October. The subject-wise timetable is attached. Students should report by 7:45 AM; papers run from 8:30 to 11:00 AM, and regular classes continue until Friday, 9 October.

Please use the parent–teacher meeting on Saturday, 3 October to discuss preparation with class teachers.

— Dr. Anita Rao, Principal"""


def seed(cmd, school):
    from apps.core import tenant

    # seed_design runs inside unscoped(), which makes every school-scoped query span *all* schools. Turn the
    # bypass off so this module only ever reads and writes SUNRISE (use_school is already active).
    token = tenant._bypass_var.set(False)
    try:
        rng = random.Random(2609)
        now = timezone.now()
        today, tz = cmd.today, cmd.tz
        _clear(cmd, school)
        guardians = _guardians(cmd, school, rng)
        _reachability(school)
        _communication(cmd, school, rng, now, today, tz, guardians)
        _documents(cmd, school, rng, now, today, tz)
        _reports(cmd, school, now, today, tz)
    finally:
        tenant._bypass_var.reset(token)


# ------------------------------------------------------------------ helpers


def _clear(cmd, school):
    phones = [cmd._phone(n) for n in range(6000, 7000)]
    Announcement.objects.filter(title__in=MY_ANNOUNCEMENTS).delete()
    Announcement.objects.filter(status=Announcement.Status.DRAFT, created_by=cmd.principal).delete()
    Notification.objects.filter(school=school, dedupe_key__startswith="announcement:").delete()
    ChannelDelivery.objects.all().delete()
    AnnouncementRead.objects.all().delete()
    SmsCreditTopUp.objects.all().delete()
    Conversation.objects.filter(id__in=Message.objects.filter(client_id__startswith="engage-").values("conversation_id")).delete()
    User.objects.filter(phone__in=phones).delete()
    PushDevice.objects.filter(token__startswith="ExponentPushToken[design-").delete()


def _at(day, hh, mm, tz):
    return datetime.combine(day, time(hh, mm), tzinfo=tz)


def _guardians(cmd, school, rng) -> list:
    """A guardian for every student without one (up to the phone range), with app, email and WhatsApp shares
    like a real school's: most families on the app, most with email, two-thirds opted in to WhatsApp."""
    guarded = set(StudentGuardian.objects.values_list("student_id", flat=True))
    students = [s for s in Student.objects.filter(is_active=True).select_related("class_group").order_by("class_group__grade", "class_group__section", "roll_no") if s.id not in guarded]
    rng.shuffle(students)
    students = students[: len(GUARDIANS)]
    users = []
    for n, student in zip(GUARDIANS, students):
        first = rng.choice(ADULT_FIRST)
        surname = student.full_name.split()[-1]
        user = User(phone=cmd._phone(n), full_name=f"{first} {surname}")
        user.set_unusable_password()
        if rng.random() < 0.86:
            user.email = f"{first.lower()}.{surname.lower()}{n % 1000}@example.in"
        user.preferences = {"channels": {"whatsapp": rng.random() < 0.67}}
        users.append(user)
    User.objects.bulk_create(users)
    Membership.objects.bulk_create([Membership(school=school, user=u, role=Role.PARENT, title="Parent") for u in users])
    StudentGuardian.objects.bulk_create(
        [StudentGuardian(school=school, student=s, user=u, relationship=rng.choice(["father", "mother"]), is_primary=True) for u, s in zip(users, students)]
    )
    return users


def _reachability(school):
    """Most parents have the app, most have an email address, two-thirds opted in to WhatsApp.

    Deterministic per phone number, so a re-run (or the People seed rebuilding guardians) gives the same shares.
    Only fills what is missing: an email or WhatsApp choice someone already has is kept."""
    import zlib

    parents = list(User.objects.filter(memberships__school=school, memberships__role=Role.PARENT, is_active=True).distinct())
    devices, changed = [], []
    for u in parents:
        h = zlib.crc32(u.phone.encode()) % 1000
        if h < 780:
            devices.append(PushDevice(user=u, token=f"ExponentPushToken[design-{u.phone[-7:]}]", platform="ios" if h % 3 == 0 else "android"))
        dirty = False
        if not u.email and h % 100 < 86:
            first, last = (u.full_name.lower().split() + ["family"])[:2]
            u.email, dirty = f"{first}.{last}{u.phone[-3:]}@example.in", True
        prefs = dict(u.preferences or {})
        channels = dict(prefs.get("channels") or {})
        if "whatsapp" not in channels:
            channels["whatsapp"] = (h * 7) % 100 < 67
            prefs["channels"] = channels
            u.preferences, dirty = prefs, True
        if dirty:
            changed.append(u)
    PushDevice.objects.bulk_create(devices, ignore_conflicts=True)
    User.objects.bulk_update(changed, ["email", "preferences"])


# ------------------------------------------------------------------ communication


def _deliver_seeded(item, school, rng, users, with_app, read_rate, channels_read=True):
    """What deliver() would have written when this went out, plus who has opened it since."""
    from apps.announcements.delivery import resolve

    r = resolve(item.audience, list(item.class_groups.all()), item.route, item.parents_only)
    reached = [u for u in (r["family"] | r["staff"]) if u.id != item.created_by_id]
    reached.sort(key=lambda u: u.phone)
    channels = set(item.channels or ["push", "in_app"])
    sent_at = item.published_at
    notes = [
        Notification(
            user=u, school=school, category="announcement", title=item.title, body=item.body[:180],
            data={"announcement_id": str(item.id), "type": "announcement"}, dedupe_key=f"announcement:{item.id}",
            push_status="sent" if ("push" in channels and u.id in with_app) else "skipped",
        )
        for u in reached
    ]
    Notification.objects.bulk_create(notes)
    Notification.objects.filter(dedupe_key=f"announcement:{item.id}").update(created_at=sent_at)
    rows = []
    for channel in ("sms", "whatsapp", "email"):
        if channel not in channels:
            continue
        for u in reached:
            if channel == "sms" and (u.id in with_app or not u.phone):
                continue
            if channel == "whatsapp" and not ((u.preferences or {}).get("channels") or {}).get("whatsapp"):
                continue
            address = u.email if channel == "email" else u.phone
            if not address:
                continue
            rows.append(ChannelDelivery(school=school, announcement=item, channel=channel, user=u, address=address, provider="log", status="failed" if rng.random() < 0.012 else "delivered"))
    ChannelDelivery.objects.bulk_create(rows)
    # Readers: app users open the notification; the rest follow the link in their SMS or email.
    readers = [u for u in reached if rng.random() < read_rate]
    opened_at = sent_at + timedelta(minutes=rng.randint(5, 240))
    app_ids = [u.id for u in readers if u.id in with_app or not channels_read]
    Notification.objects.filter(dedupe_key=f"announcement:{item.id}", user_id__in=app_ids).update(read_at=opened_at)
    emailed = {d.user_id for d in rows if d.channel == "email"}
    texted = {d.user_id for d in rows if d.channel == "sms"}
    reads = []
    for u in readers:
        if u.id in app_ids:
            continue
        channel = "sms" if u.id in texted else "email" if u.id in emailed else "in_app"
        reads.append(AnnouncementRead(school=school, announcement=item, user=u, channel=channel, read_at=opened_at))
    AnnouncementRead.objects.bulk_create(reads)
    Announcement.objects.filter(pk=item.pk).update(recipients=len(reached), delivered_at=sent_at)
    return reached, readers


def _communication(cmd, school, rng, now, today, tz, guardians):
    from apps.principal.views import GRADE_ORDER

    principal = cmd.principal
    with_app = set(PushDevice.objects.filter(is_active=True).values_list("user_id", flat=True))
    SmsCreditTopUp.objects.create(credits=20000, note="Annual SMS pack", added_by=principal)
    groups = list(ClassGroup.objects.all())
    grades = lambda lo, hi: [g for g in groups if GRADE_ORDER.index(lo) <= GRADE_ORDER.index(g.grade) <= GRADE_ORDER.index(hi)]  # noqa: E731

    def announce(title, body, audience, channels, when, rate, kind=Announcement.Kind.GENERAL, classes=(), parents_only=False, **extra):
        item = Announcement.objects.create(
            title=title, body=body, kind=kind, audience=audience, channels=channels, created_by=principal,
            published_at=when, parents_only=parents_only, **extra,
        )
        if classes:
            item.class_groups.set(classes)
        _deliver_seeded(item, school, rng, guardians, with_app, rate)
        return item

    # The base seed's notices: give them channels and read data (the PTM went out this morning).
    ptm = Announcement.objects.filter(title__startswith="Parent–teacher meeting ·").first()
    base = {
        "Route 07 running late today": (["push", "in_app", "sms"], 0.71),
        "Half-yearly invigilation roster": (["push", "in_app", "email"], 0.83),
        "Half-yearly exam timetable — Grades 6–8": (["push", "in_app", "email"], 0.88),
    }
    if ptm:
        Announcement.objects.filter(pk=ptm.pk).update(audience=Announcement.Audience.PARENTS, channels=["push", "in_app", "sms"], published_at=min(_at(today, 11, 5, tz), timezone.now() - timedelta(minutes=5)))
        ptm.refresh_from_db()
        _deliver_seeded(ptm, school, rng, guardians, with_app, 0.86)
    for item in Announcement.objects.filter(title__in=list(base), status=Announcement.Status.PUBLISHED):
        channels, rate = base[item.title]
        Announcement.objects.filter(pk=item.pk).update(channels=channels)
        item.refresh_from_db()
        _deliver_seeded(item, school, rng, guardians, with_app, rate)

    day = cmd._school_day
    announce(
        "Unit Test 2 results published", "Unit Test 2 marks are in the app under Results. Class teachers are available at the PTM to discuss them.",
        Announcement.Audience.CLASSES, ["push", "in_app", "email"], _at(day(today, -4), 16, 30, tz), 0.92,
        kind=Announcement.Kind.EXAM, classes=grades("1", "12"), parents_only=True,
    )
    announce(
        "School closed Fri 2 Oct · Gandhi Jayanti", "The school stays closed on Friday, 2 October for Gandhi Jayanti. Buses do not run.",
        Announcement.Audience.EVERYONE, ["push", "in_app", "sms", "email"], _at(day(today, -7), 10, 0, tz), 0.94, kind=Announcement.Kind.HOLIDAY,
    )
    announce(
        "Science fair registrations open", "Teams of up to three can register their project with the science department by 30 September.",
        Announcement.Audience.CLASSES, ["push", "in_app"], _at(day(today, -8), 15, 15, tz), 0.61, kind=Announcement.Kind.EVENT, classes=grades("6", "10"),
    )
    announce(
        "Annual day costume guidelines", "Costume lists for each performance are with the class teachers. Please avoid buying new outfits.",
        Announcement.Audience.CLASSES, ["push", "in_app", "whatsapp"], _at(day(today, -14), 12, 40, tz), 0.78, kind=Announcement.Kind.EVENT, classes=grades("1", "5"), parents_only=True,
    )
    announce(
        "Bus pass renewal for Term 2", "Renew bus passes in the app before 25 September to keep your child's seat.",
        Announcement.Audience.FAMILIES, ["push", "in_app", "sms", "email"], _at(day(today, -18), 9, 30, tz), 0.84, kind=Announcement.Kind.TRANSPORT,
    )

    # Circulars 13 and 14 (their PDFs are filed in Documents › Circulars › 2026–27).
    for number, title, body, ago, due, lo, hi, rate in (
        (13, "Junior dismissal at 1:45 PM", "From Monday, Nursery to Grade 5 go home at 1:45 PM. Buses leave at 1:55 PM.", 24, -18, "Nursery", "5", 0.96),
        (14, "Half-yearly exam schedule", "The half-yearly exams run 12–23 October. Please acknowledge that you've seen the schedule.", 3, 4, "6", "8", 0.72),
    ):
        item = Announcement.objects.create(
            title=title, body=body, kind=Announcement.Kind.GENERAL, audience=Announcement.Audience.CLASSES, parents_only=True,
            channels=["push", "in_app", "email"], created_by=principal, published_at=_at(day(today, -ago), 9, 0, tz),
            requires_ack=True, circular_no=number, ack_due_on=today + timedelta(days=due),
        )
        item.class_groups.set(grades(lo, hi))
        reached, _readers = _deliver_seeded(item, school, rng, guardians, with_app, min(1, rate + 0.03))
        ackers = [u for u in reached if rng.random() < rate]
        AnnouncementAck.objects.bulk_create([AnnouncementAck(school=school, announcement=item, user=u, acked_at=item.published_at + timedelta(hours=rng.randint(1, 72))) for u in ackers])

    # The draft on the composer: "Half-yearly exam timetable — Grades 6–8 · Parents", saved this afternoon.
    draft = Announcement.objects.create(
        title=DRAFT_TITLE, body=DRAFT_BODY, kind=Announcement.Kind.EXAM, audience=Announcement.Audience.CLASSES, parents_only=True,
        channels=["push", "in_app", "sms", "email"], created_by=principal, published_at=now, status=Announcement.Status.DRAFT,
    )
    draft.class_groups.set(grades("6", "8"))
    pdf = _timetable_pdf()
    draft.attachment.save("Half-yearly timetable · Gr 6–8.pdf", ContentFile(pdf), save=False)
    draft.attachment_name, draft.attachment_size = "Half-yearly timetable · Gr 6–8.pdf", len(pdf)
    draft.save()
    Announcement.objects.filter(pk=draft.pk).update(updated_at=_at(today, 14, 8, tz) if _at(today, 14, 8, tz) < now else now - timedelta(minutes=12))

    _meeting_requests(cmd, school, now, today, tz, ptm)


def _meeting_requests(cmd, school, now, today, tz, ptm):
    """Three parents asked to meet the principal; four of their messages are unread."""
    principal = cmd.principal
    kabir = Student.objects.filter(full_name="Kabir Khan", class_group__grade="6", class_group__section="B").first()
    senior = Student.objects.filter(class_group__grade="11").select_related("class_group").order_by("class_group__section", "roll_no").first()
    nazia = cmd._user(cmd._phone(6990), "Nazia Khan", school, Role.PARENT, title="Parent")
    vivek = cmd._user(cmd._phone(6991), f"Vivek {senior.full_name.split()[-1]}" if senior else "Vivek Kulkarni", school, Role.PARENT, title="Parent")
    if kabir:
        StudentGuardian.objects.get_or_create(student=kabir, user=nazia, defaults={"relationship": "mother"})
    if senior:
        StudentGuardian.objects.get_or_create(student=senior, user=vivek, defaults={"relationship": "father"})
    aarav = cmd.students.get(("6-B", "Aarav Sharma"))
    ptm_day = timezone.localtime(ptm.event_starts_at, tz).date() if ptm and ptm.event_starts_at else today + timedelta(days=7)
    rows = [
        (cmd.parent, aarav, "Route 07 delays this week", _at(cmd._school_day(today, 1), 15, 30, tz), [
            ("Good afternoon, ma'am. Route 07 has reached school after 8 AM three times this week.", timedelta(hours=26)),
            ("Could I meet you to talk about it? Monday afternoon works for me.", timedelta(hours=3)),
        ], 2),
        (nazia, kabir, "Kabir's attendance and medical leave", _at(cmd._school_day(today, 2), 9, 0, tz), [
            ("Kabir was unwell last week and has a doctor's note. I'd like to discuss his attendance.", timedelta(hours=5)),
        ], 1),
        (vivek, senior, "Fee instalment plan", _at(ptm_day, 11, 0, tz), [
            ("Could we discuss an instalment plan for the Term 2 fee during the PTM?", timedelta(minutes=40)),
        ], 1),
    ]
    for parent, student, topic, starts, thread, unread in rows:
        if student is None:
            continue
        chat = Conversation.objects.create(kind=Conversation.Kind.DIRECT, student=student)
        ConversationMember.objects.create(conversation=chat, user=parent, side="family", label=f"Parent of {student.first_name}")
        ConversationMember.objects.create(conversation=chat, user=principal, side="staff", label="Principal")
        last = None
        for index, (body, ago) in enumerate(thread):
            m = Message.objects.create(conversation=chat, sender=parent, body=body, client_id=f"engage-{chat.id}-{index}")
            Message.objects.filter(pk=m.pk).update(created_at=now - ago)
            last = (now - ago, body)
        Conversation.objects.filter(pk=chat.pk).update(last_message_at=last[0], last_message_preview=last[1][:140])
        ConversationMember.objects.filter(conversation=chat, user=parent).update(last_read_at=now)
        # The principal hasn't opened the newest ``unread`` messages.
        oldest_unread = thread[-unread][1]
        ConversationMember.objects.filter(conversation=chat, user=principal).update(last_read_at=now - oldest_unread - timedelta(minutes=1))
        Meeting.objects.create(conversation=chat, title=topic, starts_at=starts, ends_at=starts + timedelta(minutes=20), location="Principal's office", status=Meeting.Status.REQUESTED, booked_by=parent)


def _timetable_pdf() -> bytes:
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Half-yearly timetable · Grades 6–8")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, 780, "Sunrise Public School · Half-yearly examinations")
    c.setFont("Helvetica", 11)
    rows = [("Mon 12 Oct", "Mathematics"), ("Tue 13 Oct", "English"), ("Wed 14 Oct", "Science"), ("Thu 15 Oct", "Hindi"), ("Fri 16 Oct", "Social Studies"), ("Mon 19 Oct", "Computer Science")]
    y = 740
    for d, subject in rows:
        c.drawString(60, y, d)
        c.drawString(200, y, f"{subject} · 8:30–11:00 AM")
        y -= 22
    c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------------ documents


class _Backdated:
    """Let bulk_create keep the created_at we set (seeded history), then restore auto_now_add."""

    def __init__(self, model):
        self.field = model._meta.get_field("created_at")

    def __enter__(self):
        self.field.auto_now_add = False

    def __exit__(self, *exc):
        self.field.auto_now_add = True


def _pdf_pages(title: str, lines: list[str], pages: int) -> bytes:
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(title)
    for n in range(pages):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(60, 780, "Sunrise Public School")
        c.setFont("Helvetica-Bold", 13)
        c.drawString(60, 752, title)
        c.setFont("Helvetica", 11)
        y = 722
        for line in lines:
            c.drawString(60, y, line[:95])
            y -= 18
        c.setFont("Helvetica", 9)
        c.drawString(60, 40, f"Page {n + 1} of {pages}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _docx(title: str, lines: list[str]) -> bytes:
    """A minimal, valid Word document (no python-docx needed)."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    paras = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(line)}</w:t></w:r></w:p>" for line in [title, *lines])
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        f"{paras}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def _xlsx(title: str, rows: list[list]) -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = title[:30]
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _documents(cmd, school, rng, now, today, tz):
    from apps.documents.models import Document, DocumentDownload, DocumentGrant, Folder
    from apps.principal.views import GRADE_ORDER
    from apps.transport.models import Route, StudentTransport

    principal = cmd.principal
    staff = cmd.staff
    Subject = DocumentGrant.Subject
    # Remove what a previous run made (folders cascade to nothing: documents only lose their folder).
    mine = Document.objects.filter(title__in=MY_DOCUMENTS)
    for d in mine:
        d.file.delete(save=False)
    mine.delete()
    Folder.objects.all().delete()

    def folder(name, parent=None, locked=False, pos=0):
        return Folder.objects.create(name=name, parent=parent, locked=locked, position=pos, created_by=principal)

    circulars = folder("Circulars", pos=1)
    year = folder("2026–27", circulars, pos=1)
    archive = folder("Archive", circulars, pos=2)
    policies = folder("Policies", pos=2)
    reports = folder("Report cards", pos=3)
    certificates = folder("Certificates", pos=4)
    staff_records = folder("Staff records", locked=True, pos=5)
    admissions = folder("Admission documents", locked=True, pos=6)
    transport = folder("Transport", pos=7)

    # The base seed's documents go where they belong.
    Document.objects.filter(kind="circular", folder__isnull=True).update(folder=year)
    Document.objects.filter(kind="report_card", folder__isnull=True).update(folder=reports)
    Document.objects.filter(kind__in=["payslip", "certificate"], audience="private", folder__isnull=True).update(folder=staff_records)
    Document.objects.filter(kind="certificate", folder__isnull=True).update(folder=certificates)
    Document.objects.filter(kind="policy", folder__isnull=True).update(folder=policies)
    # Their "last updated" is when they were issued, not when the seed ran.
    for d in Document.objects.filter(folder__isnull=False).exclude(title__in=MY_DOCUMENTS):
        issued = d.issued_on or timezone.localtime(d.created_at, tz).date()
        Document.objects.filter(pk=d.pk).update(updated_at=_at(issued, 10, 30, tz))

    groups = {g.short_label: g for g in ClassGroup.objects.all()}
    grades = lambda lo, hi: [g for g in groups.values() if GRADE_ORDER.index(lo) <= GRADE_ORDER.index(g.grade) <= GRADE_ORDER.index(hi)]  # noqa: E731
    route07 = Route.objects.filter(code="07").first()
    guardians_of = lambda gs: list(User.objects.filter(guardian_links__student__class_group__in=gs, guardian_links__student__is_active=True).distinct().order_by("phone"))  # noqa: E731
    everyone = list(User.objects.filter(memberships__school=school, memberships__role=Role.PARENT, is_active=True).distinct().order_by("phone"))
    staff_users = list(User.objects.filter(memberships__school=school, memberships__role__in=[Role.TEACHER, Role.ADMIN, Role.ACCOUNTANT], is_active=True).distinct().order_by("phone"))
    route_parents = list(User.objects.filter(guardian_links__student__in=StudentTransport.objects.filter(route=route07, is_active=True).values("student")).distinct()) if route07 else []

    # name, description, owner, days ago, access, downloads, (pages | kind of file), extra
    specs = [
        ("Circular 14.pdf", "Half-yearly exams · 3 pages", principal, 5, ("school",), 842, 3, {}),
        ("Trip consent 6-B.pdf", "2 pages · consent due 25 Sep", staff.get("Priya Menon"), 5, ("parents", ["6-B"]), 31, 2, {"version": 2}),
        ("PTM schedule.pdf", "Sat 3 Oct · 4 pages", principal, 8, ("school",), 1026, 4, {}),
        ("Fee proposal.xlsx", "2027–28 · confidential draft", principal, 9, ("private",), 4, "xlsx", {}),
        ("Science fair.pdf", "Registrations · 1 page", staff.get("Joseph Thomas"), 12, ("parents", grades("6", "10")), 347, 1, {}),
        ("Substitutions.docx", "Cover policy · Term 1", principal, 14, ("staff",), 71, "docx", {}),
        ("Route 07 timings.pdf", "Drop-run change · 1 page", staff.get("Ramesh Gowda"), 16, ("route",), 58, 1, {}),
        ("Circular 13.pdf", "Junior dismissal 1:45 PM", principal, 18, ("parents", grades("Nursery", "5")), 612, 1, {}),
        ("Staff minutes.docx", "Meeting of 5 Sep · 3 pages", principal, 20, ("staff",), 64, "docx", {}),
        ("UT2 moderation.pdf", "Guidelines · 5 pages", staff.get("Meera Krishnan"), 22, ("staff",), 83, 5, {}),
        ("Independence Day.pdf", "Programme · 2 pages", principal, 47, ("school",), 1108, 2, {}),
        ("Circular 12.pdf", "Sports day house trials · 1 page", principal, 26, ("school",), 734, 1, {}),
        ("Circular 11.pdf", "Term 2 fee schedule · 2 pages", principal, 33, ("school",), 901, 2, {}),
        ("Circular 10.pdf", "Monsoon safety · 1 page", principal, 40, ("school",), 655, 1, {}),
    ]
    elsewhere = [
        (archive, "Circular 18 (2025–26).pdf", "Annual day · 2 pages", principal, 210, ("school",), 980, 2),
        (archive, "Circular 17 (2025–26).pdf", "Final exam timetable · 3 pages", principal, 225, ("school",), 1012, 3),
        (policies, "Child protection policy.pdf", "Reviewed Jun 2026 · 6 pages", principal, 110, ("school",), 214, 6),
        (policies, "Fee refund policy.pdf", "Board approved · 2 pages", principal, 150, ("school",), 176, 2),
        (policies, "Mobile phone policy.pdf", "Students and staff · 1 page", principal, 95, ("school",), 305, 1),
        (transport, "Bus rules and conduct.pdf", "For riders and parents · 2 pages", staff.get("Ramesh Gowda"), 70, ("route",), 41, 2),
        (admissions, "Admission checklist 2027–28.pdf", "Documents to collect · 1 page", principal, 30, ("private",), 6, 1),
        (admissions, "Fee concession applications.xlsx", "Confidential · 12 families", principal, 25, ("private",), 3, "xlsx"),
        (staff_records, "Leave policy for staff.pdf", "HR · 3 pages", principal, 120, ("staff",), 92, 3),
    ]
    downloads = []
    made = {}

    def create(where, name, description, owner, days, access, count, pages, extra=None):
        title = name.rsplit(".", 1)[0]
        ext = name.rsplit(".", 1)[-1]
        if ext == "pdf":
            data = _pdf_pages(title, [description, "Issued by the principal's office."], pages)
        elif ext == "docx":
            data = _docx(title, [description, "Circulated to all staff."])
        else:
            data = _xlsx(title, [["Item", "2026–27", "2027–28"], ["Tuition", 33500, 35800], ["Transport", 14000, 14800], ["Activities", 4500, 4800]])
        kind = access[0]
        audience = {"school": Document.Audience.EVERYONE, "staff": Document.Audience.STAFF, "parents": Document.Audience.CLASSES, "route": Document.Audience.FAMILIES, "private": Document.Audience.PRIVATE}[kind]
        when = timezone.make_aware(datetime.combine(today - timedelta(days=days), time(9 + days % 7, (days * 13) % 60)), tz)
        doc = Document(
            kind="circular" if where in (year, archive) else "policy" if where == policies else "other",
            title=title, subtitle=description, size=len(data), audience=audience, owner=owner or principal,
            issued_on=when.date(), folder=where, pages=pages if isinstance(pages, int) else 0, **(extra or {}),
        )
        doc.file.save(name, ContentFile(data), save=False)
        doc.save()
        Document.objects.filter(pk=doc.pk).update(created_at=when, updated_at=when + timedelta(hours=7, minutes=12) if name.startswith("Trip") else when)
        scope = access[1] if len(access) > 1 else []
        scope = [groups[s] if isinstance(s, str) else s for s in scope]
        if kind == "parents":
            doc.class_groups.set(scope)
        rows = {
            "school": [(Subject.STAFF, 1, 1, 0), (Subject.PARENTS, 1, 1, 0), (Subject.STUDENTS, 1, 1, 0), (Subject.ACCOUNTANT, 1, 1, 0)],
            "staff": [(Subject.STAFF, 1, 1, 0), (Subject.PARENTS, 0, 0, 0), (Subject.STUDENTS, 0, 0, 0), (Subject.ACCOUNTANT, 1, 1, 0)],
            "parents": [(Subject.CLASS_TEACHER, 1, 1, 1), (Subject.TEACHERS, 1, 1, 0), (Subject.PARENTS, 1, 1, 0), (Subject.STUDENTS, 1, 0, 0), (Subject.ACCOUNTANT, 0, 0, 0)],
            "route": [(Subject.STAFF, 1, 1, 0), (Subject.PARENTS, 1, 1, 0), (Subject.ACCOUNTANT, 0, 0, 0)],
            "private": [(Subject.STAFF, 0, 0, 0), (Subject.PARENTS, 0, 0, 0), (Subject.STUDENTS, 0, 0, 0), (Subject.ACCOUNTANT, 0, 0, 0)],
        }[kind]
        for i, (subject, v, d, u) in enumerate(rows):
            g = DocumentGrant.objects.create(document=doc, subject=subject, can_view=bool(v), can_download=bool(d), can_upload=bool(u), position=i, route=route07 if kind == "route" and subject == Subject.PARENTS else None)
            if kind == "parents" and subject not in (Subject.ACCOUNTANT,):
                g.class_groups.set(scope)
        # Who downloaded it: families in scope for family documents, staff for staff ones.
        pool = {"school": everyone, "parents": guardians_of(scope) if scope else [], "route": route_parents, "staff": staff_users, "private": [principal]}[kind] or [principal]
        users = list(pool)
        rng.shuffle(users)
        for n in range(count):
            u = users[n % len(users)]
            at = when + timedelta(minutes=rng.randint(20, max(21, int((now - when).total_seconds() // 60) - 5)))
            downloads.append(DocumentDownload(school=school, document=doc, kind="document", ref=str(doc.id), user=u, action="download", device=rng.choice(_DEVICES), created_at=at))
        made[name] = (doc, when)
        return doc

    for name, description, owner, days, access, count, pages, extra in specs:
        create(year, name, description, owner, days, access, count, pages, extra)
    for where, name, description, owner, days, access, count, pages in elsewhere:
        create(where, name, description, owner, days, access, count, pages)

    # Trip consent 6-B: this afternoon's activity (the design's "Recent access").
    trip, trip_at = made["Trip consent 6-B.pdf"]
    six_b = guardians_of([groups["6-B"]])
    viewer = next((u for u in reversed(six_b) if u.id != cmd.parent.id), cmd.parent)
    # Exactly 31 families downloaded it (the design's "31 of 38 families"), Rahul most recently.
    downloads[:] = [d for d in downloads if d.document_id != trip.id]
    for n, u in enumerate([cmd.parent] + [x for x in six_b if x.id != cmd.parent.id][:30]):
        at = _at(today, 13, 48, tz) if n == 0 else trip_at + timedelta(hours=2 + n * 3)
        downloads.append(DocumentDownload(school=school, document=trip, kind="document", ref=str(trip.id), user=u, action="download", device=rng.choice(_DEVICES), created_at=min(at, now - timedelta(minutes=1))))
    downloads.append(DocumentDownload(school=school, document=trip, kind="document", ref=str(trip.id), user=viewer, action="view", device="EduFlow Android", created_at=min(_at(today, 12, 30, tz), now - timedelta(minutes=2))))
    downloads.append(DocumentDownload(school=school, document=trip, kind="document", ref=str(trip.id), user=staff.get("Priya Menon"), action="permissions", device="Chrome · macOS", created_at=trip_at + timedelta(hours=7, minutes=12)))
    with _Backdated(DocumentDownload):
        DocumentDownload.objects.bulk_create(downloads, batch_size=500)


_DEVICES = ["EduFlow Android", "EduFlow Android", "EduFlow iOS", "Chrome · Windows", "Safari · iPhone", "Chrome · macOS"]
MY_DOCUMENTS = [
    "Circular 14", "Trip consent 6-B", "PTM schedule", "Fee proposal", "Science fair", "Substitutions", "Route 07 timings", "Circular 13",
    "Staff minutes", "UT2 moderation", "Independence Day", "Circular 12", "Circular 11", "Circular 10", "Circular 18 (2025–26)",
    "Circular 17 (2025–26)", "Child protection policy", "Fee refund policy", "Mobile phone policy", "Bus rules and conduct",
    "Admission checklist 2027–28", "Fee concession applications", "Leave policy for staff",
]


# ------------------------------------------------------------------ reports


def _reports(cmd, school, now, today, tz):
    from datetime import time as dtime

    from apps.reports import services
    from apps.reports.models import CustomReport, ReportDelivery, ReportRun, ScheduledReport

    for run in ReportRun.objects.all():
        run.file.delete(save=False)
    ReportRun.objects.all().delete()
    ScheduledReport.objects.all().delete()
    CustomReport.objects.all().delete()

    principal = cmd.principal
    office = User.objects.filter(phone=cmd._phone(42)).first()  # Shalini Rao, school office
    accountant = User.objects.filter(phone=cmd._phone(40)).first()  # Deepak Nair
    transport = User.objects.filter(phone=cmd._phone(41)).first()  # Ramesh Gowda
    f = services.default_filters(school)
    day = cmd._school_day
    first_of_month = today.replace(day=1)
    while first_of_month.weekday() == 6:
        first_of_month += timedelta(days=1)

    # "Last generated" on every library card: when someone last pressed Generate.
    for key, fmt, when in (
        ("attendance_register", "pdf", _at(day(today, -4), 7, 5, tz)),
        ("class_performance", "xlsx", _at(day(today, -8), 16, 40, tz)),
        ("fee_collection", "xlsx", _at(day(today, -4), 13, 0, tz)),
        ("defaulters", "xlsx", _at(day(today, -8), 17, 0, tz)),
        ("staff_attendance", "pdf", _at(first_of_month, 8, 0, tz)),
        ("transport_utilisation", "pdf", _at(day(today, -11), 18, 0, tz)),
        ("admissions_funnel", "pdf", _at(day(today, -5), 9, 30, tz)),
    ):
        run = services.generate(school, key, fmt, f, user=principal if key != "fee_collection" else accountant)
        ReportRun.objects.filter(pk=run.pk).update(created_at=when, updated_at=when)

    def schedule(name, report, fmt, frequency, at, recipients, enabled=True, weekday=0, day_of_month=1, history=()):
        s = ScheduledReport.objects.create(
            name=name, report=report, format=fmt, frequency=frequency, at=at, weekday=weekday, day_of_month=day_of_month,
            enabled=enabled, created_by=principal,
        )
        s.recipients.set([u for u in recipients if u])
        # Past runs, delivered in-app and by email (the Delivery log).
        for when in history:
            run = services.generate(school, report, fmt, f, schedule=s)
            ReportRun.objects.filter(pk=run.pk).update(created_at=when, updated_at=when)
            rows = []
            for u in s.recipients.all():
                rows.append(ReportDelivery(school=school, schedule=s, run=run, user=u, channel="in_app", status="delivered", created_at=when))
                if u.email:
                    rows.append(ReportDelivery(school=school, schedule=s, run=run, user=u, channel="email", address=u.email, status="logged", created_at=when))
            with _Backdated(ReportDelivery):
                ReportDelivery.objects.bulk_create(rows)
        last = max(history) if history else None
        ScheduledReport.objects.filter(pk=s.pk).update(last_run_at=last, next_run_at=services.next_run(s, now) if enabled else None)
        return s

    def last_weekday(wd, hh, mm, n=3):
        out, d = [], today
        while len(out) < n:
            if d.weekday() == wd and _at(d, hh, mm, tz) < now:
                out.append(_at(d, hh, mm, tz))
            d -= timedelta(days=1)
        return out

    schedule("Weekly attendance digest", "attendance_register", "pdf", "weekly", dtime(7, 0), [principal, office], weekday=0, history=last_weekday(0, 7, 0))
    schedule("Fee defaulters summary", "defaulters", "xlsx", "weekly", dtime(17, 0), [principal, accountant], weekday=4, history=last_weekday(4, 17, 0))
    schedule("Staff attendance muster", "staff_attendance", "xlsx", "monthly", dtime(8, 0), [principal, office], day_of_month=1, history=[_at(first_of_month, 8, 0, tz)])
    schedule("Transport punctuality", "transport_utilisation", "pdf", "daily", dtime(18, 0), [transport], enabled=False, history=[_at(day(today, -12), 18, 0, tz)])
