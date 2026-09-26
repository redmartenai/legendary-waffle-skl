"""Console: timetable page. The live week per class, teacher or room; draft edits with clash checks; publishing;
and today's substitutions."""

import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import ClassGroup, Student, Subject, TeachingAssignment, TimetableDraft, TimetableRevision, TimetableSlot
from apps.accounts.audit import audit
from apps.core.api import SchoolAPIView, parse_uuid
from apps.core.utils import school_now, school_today, school_tz
from apps.notifications.models import Category
from apps.notifications.services import notify
from apps.principal.views import _grade_key, cover_board, staff_on_leave
from apps.staff.models import Substitution
from apps.staff.services import BREAK_MINUTES, LUNCH_MINUTES, school_periods

from .academics import person, teacher_subjects, teachers
from .common import CONSOLE_ROLES, current_term

# Rooms several classes can use at once (the sports ground) never clash.
SHARED_ROOMS = {"Ground"}
DAYS = 6  # Monday–Saturday
ASSEMBLY_STARTS = "08:00"


def _hm(t) -> str:
    return t.strftime("%H:%M") if t else ""


def _week_of(day: date) -> list[date]:
    monday = day - timedelta(days=day.weekday())
    return [monday + timedelta(days=i) for i in range(DAYS)]


def bell_schedule() -> dict:
    """Periods per weekday, the breaks between them, and assembly before the first bell."""
    days = {wd: school_periods(wd) for wd in range(DAYS)}
    longest = max(days.values(), key=len, default=[])
    rows, prev = [], None
    for period, starts, ends in longest:
        if prev is not None:
            gap = (datetime.strptime(starts, "%H:%M") - datetime.strptime(prev, "%H:%M")).seconds // 60
            if gap >= BREAK_MINUTES:
                rows.append({"kind": "lunch" if gap >= LUNCH_MINUTES else "break", "starts_at": prev, "ends_at": starts})
        rows.append({"kind": "period", "period": period, "starts_at": starts, "ends_at": ends})
        prev = ends
    first = longest[0][1] if longest else None
    return {
        "assembly": {"starts_at": ASSEMBLY_STARTS, "ends_at": first} if first and first > ASSEMBLY_STARTS else None,
        "rows": rows,
        "periods_by_day": {wd: [p for p, _s, _e in d] for wd, d in days.items()},
        "ends_by_day": {wd: d[-1][2] if d else None for wd, d in days.items()},
    }


# ------------------------------------------------------------------ the effective week: live + draft


def effective_cells() -> dict:
    """Every class period as it would be if the draft were published: (class_id, weekday, period) → cell."""
    cells = {}
    for s in TimetableSlot.objects.select_related("subject", "teacher", "class_group"):
        cells[(s.class_group_id, s.weekday, s.period)] = {
            "slot_id": s.id, "class": s.class_group, "subject": s.subject, "teacher": s.teacher, "room": s.room, "draft": None,
        }
    for d in TimetableDraft.objects.select_related("subject", "teacher", "class_group"):
        key = (d.class_group_id, d.weekday, d.period)
        live = cells.get(key)
        cells[key] = {
            "slot_id": live["slot_id"] if live else None, "class": d.class_group, "subject": d.subject, "teacher": d.teacher, "room": d.room,
            "draft": d, "live": live,
        }
        if d.subject_id is None:
            cells[key]["subject"] = None
    return cells


def find_clashes(cells: dict) -> dict:
    """Draft periods that put a teacher or a room in two places at once: (class_id, weekday, period) → clash."""
    by_teacher, by_room = defaultdict(list), defaultdict(list)
    for key, c in cells.items():
        if c["subject"] is None:
            continue
        _cid, wd, p = key
        if c["teacher"] is not None:
            by_teacher[(c["teacher"].id, wd, p)].append(key)
        if c["room"] and c["room"] not in SHARED_ROOMS:
            by_room[(c["room"], wd, p)].append(key)
    out = {}
    for kind, index in (("teacher", by_teacher), ("room", by_room)):
        for keys in index.values():
            if len(keys) < 2:
                continue
            for key in keys:
                if cells[key]["draft"] is None or key in out:
                    continue
                other = next(k for k in keys if k != key)
                oc = cells[other]
                out[key] = {"kind": kind, "class": oc["class"].short_label, "subject": oc["subject"].name if oc["subject"] else None}
    return out


def _cell_payload(c, clash=None) -> dict:
    subject = c["subject"]
    return {
        "subject": {"id": str(subject.id), "code": subject.code, "name": subject.name} if subject else None,
        "teacher": person(c["teacher"]),
        "room": c["room"],
        "class": {"id": str(c["class"].id), "label": c["class"].short_label},
        "slot_id": str(c["slot_id"]) if c.get("slot_id") else None,
        "draft": (
            {"batch": str(c["draft"].batch), "kind": c["draft"].kind, "cleared": subject is None}
            if c["draft"] is not None
            else None
        ),
        "clash": clash,
    }


def draft_summary(school, cells=None) -> dict:
    """What's in the draft: edited periods, batches, and each clash with the date it would first bite."""
    cells = cells if cells is not None else effective_cells()
    clashes = find_clashes(cells)
    drafts = list(TimetableDraft.objects.select_related("class_group"))
    today = school_today(school)
    bells = {wd: {p: (s, e) for p, s, e in school_periods(wd)} for wd in range(DAYS)}
    held = {str(cells[k]["draft"].batch) for k in clashes}
    items = []
    for key, clash in sorted(clashes.items(), key=lambda kv: (kv[0][1], kv[0][2])):
        c = cells[key]
        _cid, wd, p = key
        when = today + timedelta(days=(wd - today.weekday()) % 7)
        batch = [d for d in drafts if d.batch == c["draft"].batch]
        source = None
        if c["draft"].kind == "swap":
            partner = next((d for d in batch if (d.weekday, d.period) != (wd, p)), None)
            if partner:
                source = {"weekday": partner.weekday, "period": partner.period}
        items.append(
            {
                "batch": str(c["draft"].batch),
                "kind": c["draft"].kind,
                "class": {"id": str(c["class"].id), "label": c["class"].short_label},
                "weekday": wd,
                "period": p,
                "date": when.isoformat(),
                "starts_at": bells.get(wd, {}).get(p, ("", ""))[0],
                "subject": c["subject"].name if c["subject"] else None,
                "code": c["subject"].code if c["subject"] else None,
                "teacher": person(c["teacher"]),
                "room": c["room"],
                "with": clash,
                "from": source,
            }
        )
    rev = TimetableRevision.objects.order_by("-published_at").first()
    return {
        "periods": len(drafts),
        "batches": len({d.batch for d in drafts}),
        "clashes": items,
        "held_batches": sorted(held),
        "publishable": len({d.batch for d in drafts} - {uuid.UUID(b) for b in held}),
        "live_since": timezone.localtime(rev.published_at, school_tz(school)).date().isoformat() if rev else None,
    }


# ------------------------------------------------------------------ views


def _options():
    groups = sorted(ClassGroup.objects.all(), key=lambda g: (_grade_key(g.grade), g.section))
    staff = sorted(teachers().values(), key=lambda u: u.full_name)
    rooms = sorted({r for r in TimetableSlot.objects.values_list("room", flat=True) if r}, key=lambda r: (not r.startswith("Room"), r))
    return {
        "classes": [{"id": str(g.id), "label": g.short_label} for g in groups],
        "teachers": [{"id": str(u.id), "label": u.full_name} for u in staff],
        "rooms": [{"id": r, "label": r} for r in rooms],
    }


class TimetableView(SchoolAPIView):
    """GET ?view=class|teacher|room&id=…&week=YYYY-MM-DD — one week of the timetable, draft included."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        today = school_today(school)
        now = school_now(school)
        view = request.query_params.get("view") or "class"
        if view not in ("class", "teacher", "room"):
            raise ValidationError({"view": "Use class, teacher or room."})
        raw_week = request.query_params.get("week")
        try:
            anchor = date.fromisoformat(raw_week) if raw_week else today
        except ValueError as exc:
            raise ValidationError({"week": "Use YYYY-MM-DD."}) from exc
        days = _week_of(anchor)
        options = _options()
        target_id = request.query_params.get("id")
        cells = effective_cells()
        clashes = find_clashes(cells)
        bells = bell_schedule()

        meta, target = {}, None
        if view == "class":
            groups = {str(g.id): g for g in ClassGroup.objects.select_related("class_teacher")}
            if target_id and target_id not in groups:
                raise Http404
            g = groups.get(target_id) if target_id else None
            if g is None:
                g = next((x for x in groups.values() if x.short_label == "6-B"), None) or next(iter(groups.values()), None)
            if g is None:
                raise Http404
            target = {"id": str(g.id), "label": g.short_label}
            mine = {k: c for k, c in cells.items() if k[0] == g.id}
            live = TimetableSlot.objects.filter(class_group=g)
            rooms = Counter(r for r in live.values_list("room", flat=True) if r.startswith("Room"))
            meta = {
                "class_teacher": person(g.class_teacher),
                "room": rooms.most_common(1)[0][0] if rooms else None,
                "students": Student.objects.filter(class_group=g, is_active=True).count(),
                "periods": live.count(),
            }
            subs = Substitution.objects.filter(date__in=days, slot__class_group=g)
        elif view == "teacher":
            staff = teachers()
            uid = parse_uuid(target_id, "id") if target_id else None
            u = staff.get(uid) if uid else sorted(staff.values(), key=lambda x: x.full_name)[0] if staff else None
            if u is None:
                raise Http404
            target = {"id": str(u.id), "label": u.full_name}
            mine = {k: c for k, c in cells.items() if c["teacher"] is not None and c["teacher"].id == u.id and c["subject"] is not None}
            meta = {"periods": len(mine), "subjects": sorted({c["subject"].name for c in mine.values()})}
            subs = Substitution.objects.filter(date__in=days, teacher=u) | Substitution.objects.filter(date__in=days, absent_teacher=u)
        else:
            rooms = [o["id"] for o in options["rooms"]]
            r = target_id if target_id in rooms else (rooms[0] if rooms else None)
            if r is None:
                raise Http404
            target = {"id": r, "label": r}
            mine = {k: c for k, c in cells.items() if c["room"] == r and c["subject"] is not None}
            meta = {"periods": len(mine)}
            subs = Substitution.objects.filter(date__in=days, slot__room=r)

        sub_by_cell = {}
        for s in subs.select_related("teacher", "absent_teacher", "slot"):
            sub_by_cell[(s.slot.class_group_id, s.slot.weekday, s.slot.period)] = {
                "date": s.date.isoformat(), "teacher": person(s.teacher), "for": person(s.absent_teacher), "reason": s.reason,
            }

        out = []
        for key, c in mine.items():
            cid, wd, p = key
            if view != "class" and c["draft"] is not None and c["subject"] is None:
                continue
            item = {"weekday": wd, "period": p, **_cell_payload(c, clashes.get(key))}
            item["sub"] = sub_by_cell.get(key)
            # The live period this draft replaces, for the tooltip and the undo.
            if c["draft"] is not None and c.get("live"):
                lv = c["live"]
                item["live"] = {"subject": lv["subject"].name if lv["subject"] else None, "teacher": person(lv["teacher"]), "room": lv["room"]}
            out.append(item)
        # Draft periods that moved a class out of this teacher's or room's week still show as edits on the class.
        now_cell = None
        if today in days:
            for p, starts, ends in school_periods(today.weekday()):
                if starts <= now.strftime("%H:%M") < ends:
                    now_cell = {"weekday": today.weekday(), "period": p}
        return Response(
            {
                "view": view,
                "target": target,
                "meta": meta,
                "options": options,
                "today": today.isoformat(),
                "term": current_term(school, today),
                "week": {
                    "start": days[0].isoformat(),
                    "end": days[-1].isoformat(),
                    "days": [{"date": d.isoformat(), "weekday": d.weekday(), "today": d == today, "periods": bells["periods_by_day"].get(d.weekday(), [])} for d in days],
                },
                "bells": {"assembly": bells["assembly"], "rows": bells["rows"], "ends_by_day": {str(k): v for k, v in bells["ends_by_day"].items()}},
                "now": now_cell,
                "cells": sorted(out, key=lambda c: (c["weekday"], c["period"])),
                "draft": draft_summary(school, cells),
                "subjects": [{"id": str(s.id), "code": s.code, "name": s.name} for s in Subject.objects.order_by("name")],
            }
        )


def _class_or_404(raw) -> ClassGroup:
    g = ClassGroup.objects.filter(id=parse_uuid(raw, "class_id")).first()
    if g is None:
        raise Http404
    return g


def _slot_ref(data, prefix="") -> tuple[int, int]:
    try:
        wd, p = int(data.get(f"{prefix}weekday")), int(data.get(f"{prefix}period"))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"period": "Choose a period."}) from exc
    if not 0 <= wd < DAYS or p not in [x for x, _s, _e in school_periods(wd)]:
        raise ValidationError({"period": "There's no such period that day."})
    return wd, p


class DraftChangeView(SchoolAPIView):
    """POST: change one period in the draft (subject, teacher, room; no subject clears it)."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        g = _class_or_404(request.data.get("class_id"))
        wd, p = _slot_ref(request.data)
        subject = None
        if request.data.get("subject_id"):
            subject = Subject.objects.filter(id=parse_uuid(request.data["subject_id"], "subject_id")).first()
            if subject is None:
                raise ValidationError({"subject_id": "Unknown subject."})
        teacher = None
        if request.data.get("teacher_id"):
            teacher = teachers().get(parse_uuid(request.data["teacher_id"], "teacher_id"))
            if teacher is None:
                raise ValidationError({"teacher_id": "Choose a teacher."})
        elif subject is not None:
            a = TeachingAssignment.objects.filter(class_group=g, subject=subject).select_related("teacher").first()
            teacher = a.teacher if a else None
        live = TimetableSlot.objects.filter(class_group=g, weekday=wd, period=p).first()
        room = str(request.data.get("room") if request.data.get("room") is not None else (live.room if live else "")).strip()[:30]
        if live and subject and live.subject_id == subject.id and live.teacher_id == (teacher.id if teacher else None) and live.room == room:
            TimetableDraft.objects.filter(class_group=g, weekday=wd, period=p).delete()
            return Response(draft_summary(request.school))
        with transaction.atomic():
            TimetableDraft.objects.filter(class_group=g, weekday=wd, period=p).delete()
            TimetableDraft.objects.create(
                class_group=g, weekday=wd, period=p, subject=subject, teacher=teacher if subject else None, room=room if subject else "",
                batch=uuid.uuid4(), kind="change", created_by=request.user,
            )
        return Response(draft_summary(request.school), status=201)


def _swap(request, g, a, b, batch=None):
    """Swap two periods of one class in the draft (their current, possibly already drafted, contents)."""
    if a == b:
        raise ValidationError({"period": "Choose two different periods."})
    cells = effective_cells()
    ca, cb = cells.get((g.id, *a)), cells.get((g.id, *b))
    if ca is None and cb is None:
        raise ValidationError({"period": "Both periods are free."})
    batch = batch or uuid.uuid4()
    TimetableDraft.objects.filter(class_group=g).filter(Q(weekday=a[0], period=a[1]) | Q(weekday=b[0], period=b[1])).delete()
    for (wd, p), src in ((a, cb), (b, ca)):
        live = TimetableSlot.objects.filter(class_group=g, weekday=wd, period=p).first()
        subject = src["subject"] if src else None
        teacher = src["teacher"] if src else None
        room = src["room"] if src else ""
        if live and subject and live.subject_id == subject.id and live.teacher_id == (teacher.id if teacher else None) and live.room == room:
            continue
        TimetableDraft.objects.create(class_group=g, weekday=wd, period=p, subject=subject, teacher=teacher, room=room, batch=batch, kind="swap", created_by=request.user)


class DraftSwapView(SchoolAPIView):
    """POST {class_id, weekday, period, to_weekday, to_period}: swap two periods in the draft."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        g = _class_or_404(request.data.get("class_id"))
        a = _slot_ref(request.data)
        b = _slot_ref(request.data, "to_")
        with transaction.atomic():
            _swap(request, g, a, b)
        return Response(draft_summary(request.school), status=201)


def _batch_or_404(batch) -> list[TimetableDraft]:
    rows = list(TimetableDraft.objects.filter(batch=parse_uuid(batch, "batch")).select_related("class_group"))
    if not rows:
        raise Http404
    return rows


def _original_swap(rows) -> tuple:
    """For a swap batch, the two periods it exchanged."""
    if len(rows) != 2:
        raise ValidationError({"batch": "Only a swap can be moved."})
    return (rows[0].weekday, rows[0].period), (rows[1].weekday, rows[1].period)


class DraftBatchView(SchoolAPIView):
    """DELETE: undo one draft edit (both halves of a swap)."""

    allowed_roles = CONSOLE_ROLES

    def delete(self, request, batch):
        rows = _batch_or_404(batch)
        TimetableDraft.objects.filter(id__in=[r.id for r in rows]).delete()
        return Response(draft_summary(request.school))


class FreeSlotsView(SchoolAPIView):
    """GET: other periods the clashing half of a swap could go to without any clash. POST {weekday, period}: redo the swap there."""

    allowed_roles = CONSOLE_ROLES

    def _moving(self, rows):
        """The period whose content causes the clash, and where that content came from."""
        cells = effective_cells()
        clashes = find_clashes(cells)
        a, b = _original_swap(rows)
        g = rows[0].class_group
        if (g.id, *a) in clashes:
            return g, b, a
        if (g.id, *b) in clashes:
            return g, a, b
        raise ValidationError({"batch": "This edit has no clash."})

    def get(self, request, batch):
        rows = _batch_or_404(batch)
        g, origin, _clashing = self._moving(rows)
        # Try the swap from the original period with every other period of the week, on a scratch copy.
        live = {(s.weekday, s.period): s for s in TimetableSlot.objects.filter(class_group=g).select_related("subject", "teacher")}
        others = {k: c for k, c in effective_cells().items() if k[0] != g.id or (k[1], k[2]) not in [(r.weekday, r.period) for r in rows]}
        busy_t, busy_r = defaultdict(set), defaultdict(set)
        for (cid, wd, p), c in others.items():
            if cid == g.id or c["subject"] is None:
                continue
            if c["teacher"]:
                busy_t[c["teacher"].id].add((wd, p))
            if c["room"] and c["room"] not in SHARED_ROOMS:
                busy_r[c["room"]].add((wd, p))
        mine = {(wd, p): c for (cid, wd, p), c in others.items() if cid == g.id}
        mover = live.get(origin)
        if mover is None:
            return Response({"items": []})
        bells = {wd: {p: s for p, s, _e in school_periods(wd)} for wd in range(DAYS)}
        items = []
        for (wd, p), c in sorted(mine.items()):
            if (wd, p) == origin or c["subject"] is None or c["draft"] is not None or c["subject"].id == mover.subject_id:
                continue
            ok_mover = (mover.teacher_id is None or (wd, p) not in busy_t[mover.teacher_id]) and (not mover.room or mover.room in SHARED_ROOMS or (wd, p) not in busy_r[mover.room])
            ok_back = (c["teacher"] is None or origin not in busy_t[c["teacher"].id]) and (not c["room"] or c["room"] in SHARED_ROOMS or origin not in busy_r[c["room"]])
            if ok_mover and ok_back:
                items.append({"weekday": wd, "period": p, "starts_at": bells[wd].get(p), "subject": c["subject"].name, "teacher": person(c["teacher"])})
        return Response({"subject": mover.subject.name, "from": {"weekday": origin[0], "period": origin[1]}, "items": items[:12]})

    def post(self, request, batch):
        rows = _batch_or_404(batch)
        g, origin, _clashing = self._moving(rows)
        target = _slot_ref(request.data)
        with transaction.atomic():
            TimetableDraft.objects.filter(id__in=[r.id for r in rows]).delete()
            _swap(request, g, origin, target, batch=rows[0].batch)
        summary = draft_summary(request.school)
        if rows[0].batch.hex in {uuid.UUID(b).hex for b in summary["held_batches"]}:
            raise ValidationError({"period": "That period clashes too."})
        return Response(summary)


class PublishView(SchoolAPIView):
    """Apply every draft edit without a clash to the live timetable; clashing edits stay in the draft."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        school = request.school
        summary = draft_summary(school)
        held = {uuid.UUID(b) for b in summary["held_batches"]}
        rows = [d for d in TimetableDraft.objects.select_related("class_group", "subject", "teacher") if d.batch not in held]
        if not rows:
            raise ValidationError({"draft": "Nothing to publish." if not held else "Every change in the draft has a clash. Fix it first."})
        bells = {wd: {p: (s, e) for p, s, e in school_periods(wd)} for wd in range(DAYS)}
        affected = defaultdict(list)
        with transaction.atomic():
            for d in rows:
                live = TimetableSlot.objects.filter(class_group=d.class_group, weekday=d.weekday, period=d.period).select_related("teacher").first()
                label = f"{d.class_group.short_label} {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.weekday]} P{d.period}"
                if live and live.teacher_id:
                    affected[live.teacher].append(label)
                if d.teacher_id:
                    affected[d.teacher].append(label)
                if d.subject_id is None:
                    if live:
                        live.delete()
                    continue
                starts, ends = bells.get(d.weekday, {}).get(d.period, (None, None))
                if live is None and starts is None:
                    continue
                values = {"subject": d.subject, "teacher": d.teacher, "room": d.room}
                if live:
                    for k, v in values.items():
                        setattr(live, k, v)
                    live.save(update_fields=["subject", "teacher", "room", "updated_at"])
                else:
                    TimetableSlot.objects.create(
                        class_group=d.class_group, weekday=d.weekday, period=d.period, starts_at=datetime.strptime(starts, "%H:%M").time(), ends_at=datetime.strptime(ends, "%H:%M").time(), **values
                    )
            TimetableDraft.objects.filter(id__in=[d.id for d in rows]).delete()
            TimetableRevision.objects.create(published_by=request.user, published_at=timezone.now(), changes=len(rows), summary=", ".join(sorted({d.class_group.short_label for d in rows})))
        for user, labels in affected.items():
            notify(
                [user],
                school=school,
                category=Category.GENERAL,
                title="Your timetable has changed",
                body=", ".join(sorted(set(labels)))[:180],
                data={"type": "timetable"},
            )
        audit(request, "timetable.publish", summary=f"{len(rows)} periods · {len(affected)} teachers told · {len(held)} held back")
        return Response({"applied": len(rows), "held": len(held), "notified": len(affected), "draft": draft_summary(school)})


# ------------------------------------------------------------------ substitutions today


def cover_today(school) -> dict:
    """Periods today whose teacher is on leave: covered or not, and a suggested free teacher for each open one."""
    today = school_today(school)
    board = cover_board(school, today)
    quals = teacher_subjects()
    subject_ids = {s.name: s.id for s in Subject.objects.all()}
    codes = dict(Subject.objects.values_list("name", "code"))
    load_today = Counter(TimetableSlot.objects.filter(weekday=today.weekday()).exclude(teacher=None).values_list("teacher_id", flat=True))
    now = school_now(school)
    items, suggested = [], set()
    for period in board["periods"]:
        for slot in period["slots"]:
            if slot["covered_by"]:
                continue
            sid = subject_ids.get(slot["subject"])
            # The same subject first, then whoever teaches least today; one suggestion per teacher per period.
            free = sorted(
                (f for f in period["free"] if (f["id"], period["period"]) not in suggested),
                key=lambda f: (sid not in quals.get(uuid.UUID(f["id"]), set()), load_today.get(uuid.UUID(f["id"]), 0), f["name"]),
            )
            pick = free[0] if free and period["state"] != "done" else None
            if pick:
                suggested.add((pick["id"], period["period"]))
            starts = datetime.combine(today, datetime.strptime(period["starts_at"], "%H:%M").time(), tzinfo=school_tz(school))
            items.append(
                {
                    "slot_id": slot["id"],
                    "class": slot["class"],
                    "subject": slot["subject"],
                    "code": codes.get(slot["subject"], ""),
                    "period": period["period"],
                    "starts_at": period["starts_at"],
                    "state": period["state"],
                    "minutes": max(0, int((starts - now).total_seconds() // 60)) if period["state"] == "todo" else None,
                    "for": next(({"name": lv["name"], "initials": lv["initials"]} for lv in board["on_leave"] if lv["name"] == slot["teacher"]), {"name": slot["teacher"], "initials": ""}),
                    "suggestion": {"id": pick["id"], "name": pick["name"], "initials": "".join(w[0] for w in pick["name"].split()[:2]).upper()} if pick else None,
                }
            )
    open_items = [i for i in items if i["state"] != "done"]
    return {
        "date": today.isoformat(),
        "on_leave": [{"name": lv["name"], "initials": lv["initials"]} for lv in board["on_leave"]],
        "items": items,
        "open": len(open_items),
        "suggestions": sum(1 for i in open_items if i["suggestion"]),
        "day_over": bool(items) and not open_items,
    }


def assign_cover(request, slot_id, teacher_id) -> Substitution:
    """The principal/cover rules: today's period of a teacher on leave, not already covered, to a teacher free then."""
    from apps.accounts.models import Membership, Role

    today = school_today(request.school)
    slot = TimetableSlot.objects.filter(id=parse_uuid(slot_id, "slot_id")).select_related("class_group", "subject", "teacher").first()
    if slot is None or slot.weekday != today.weekday():
        raise Http404
    if slot.teacher_id not in {lv.user_id for lv in staff_on_leave(today)}:
        raise ValidationError({"slot_id": "This period's teacher isn't on leave today."})
    if Substitution.objects.filter(date=today, slot=slot).exists():
        raise ValidationError({"slot_id": "This period already has cover."})
    teacher = next((m.user for m in Membership.objects.filter(user_id=parse_uuid(teacher_id, "teacher_id"), role=Role.TEACHER, is_active=True).select_related("user")), None)
    if teacher is None:
        raise ValidationError({"teacher_id": "Choose a teacher."})
    if teacher.id in {lv.user_id for lv in staff_on_leave(today)}:
        raise ValidationError({"teacher_id": f"{teacher.full_name} is on leave today."})
    clash = TimetableSlot.objects.filter(teacher=teacher, weekday=today.weekday(), period=slot.period).exists() or Substitution.objects.filter(
        date=today, teacher=teacher, slot__period=slot.period
    ).exists()
    if clash:
        raise ValidationError({"teacher_id": f"{teacher.full_name} is teaching then."})
    sub = Substitution.objects.create(
        date=today, slot=slot, teacher=teacher, absent_teacher=slot.teacher, reason="on leave", assigned_by=request.user, assigned_at=timezone.now()
    )
    notify(
        [teacher],
        school=request.school,
        category=Category.GENERAL,
        title=f"Cover: {slot.class_group.short_label} {slot.subject.name}, P{slot.period}",
        body=f"For {slot.teacher.full_name} · {slot.starts_at:%I:%M %p} · {slot.room}".replace(" 0", " "),
        data={"type": "cover", "cover_id": str(sub.id)},
    )
    return sub


class CoverView(SchoolAPIView):
    """GET today's substitutions; POST {slot_id, teacher_id} assigns one; POST {all: true} takes every suggestion."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        return Response(cover_today(request.school))

    def post(self, request):
        if request.data.get("all"):
            board = cover_today(request.school)
            done = 0
            with transaction.atomic():
                for item in board["items"]:
                    if item["state"] != "done" and item["suggestion"]:
                        assign_cover(request, item["slot_id"], item["suggestion"]["id"])
                        done += 1
            if not done:
                raise ValidationError({"all": "There are no suggestions to take."})
            audit(request, "timetable.cover", summary=f"{done} periods covered")
        else:
            sub = assign_cover(request, request.data.get("slot_id"), request.data.get("teacher_id"))
            audit(request, "timetable.cover", target=sub, summary=f"{sub.slot.class_group.short_label} P{sub.slot.period} → {sub.teacher.full_name}")
        return Response(cover_today(request.school), status=201)


urlpatterns = [
    path("console/timetable", TimetableView.as_view()),
    path("console/timetable/draft", DraftChangeView.as_view()),
    path("console/timetable/draft/swap", DraftSwapView.as_view()),
    path("console/timetable/draft/<str:batch>", DraftBatchView.as_view()),
    path("console/timetable/draft/<str:batch>/free-slots", FreeSlotsView.as_view()),
    path("console/timetable/publish", PublishView.as_view()),
    path("console/timetable/cover", CoverView.as_view()),
]
