"""Console: admissions — the pipeline funnel, seats by grade and the board from enquiry to admission."""

import uuid
from collections import Counter
from datetime import date, datetime, timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Max, Q
from django.http import Http404
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.academics.models import ClassGroup
from apps.accounts.audit import audit
from apps.admissions.models import AdmissionCycle, Application, ApplicationEvent, SeatPlan
from apps.approvals.models import ApprovalRequest
from apps.core.api import SchoolAPIView
from apps.core.utils import normalize_phone, school_today, school_tz
from apps.principal.views import _grade_key, _pct

from .common import CONSOLE_ROLES

STAGES = Application.ORDER
GRADES = ["Nursery", "LKG", "UKG"] + [str(g) for g in range(1, 13)]


def current_cycle() -> AdmissionCycle | None:
    return AdmissionCycle.objects.filter(is_current=True).order_by("-session_starts_on").first()


def _pending_requests(apps) -> dict:
    """application id -> pending approval request id."""
    ct = ContentType.objects.get_for_model(Application)
    return {
        r.target_id: r.id
        for r in ApprovalRequest.objects.filter(kind="admission", status=ApprovalRequest.Status.PENDING, target_type=ct, target_id__in=[a.id for a in apps])
    }


def _tag(app: Application, tz) -> dict:
    """What the card's coloured strip says, as data the page words itself."""
    s = app.stage
    if s == "enquiry":
        return {"kind": app.follow_up or "new", "date": app.follow_up_on.isoformat() if app.follow_up_on else None}
    if s == "application":
        if app.documents_pending:
            return {"kind": "document_due", "what": app.documents_pending}
        return {"kind": "form_fee_paid"} if app.form_fee_paid else {"kind": "book_assessment"}
    if s == "assessment":
        if app.assessment_score is not None:
            return {"kind": "scored", "score": app.assessment_score, "out_of": app.assessment_out_of}
        if app.assessment_at:
            return {"kind": "slot", "at": app.assessment_at.astimezone(tz).isoformat()}
        return {"kind": "book_slot"}
    if s == "documents":
        if app.documents_verified:
            return {"kind": "verified"}
        return {"kind": "document_pending", "what": app.documents_pending or "Documents"}
    if s == "offer":
        if app.offer_accepted_on:
            return {"kind": "fee_due", "date": app.fee_due_on.isoformat() if app.fee_due_on else None}
        return {"kind": "reply_by", "date": app.offer_reply_by.isoformat() if app.offer_reply_by else None}
    return {"kind": "fee_paid", "receipt": app.fee_receipt_no}


def card(app: Application, tz, pending: dict) -> dict:
    when = app.stage_changed_at or app.created_at
    slot_day = app.stage == "assessment" and app.assessment_score is None and app.assessment_at
    return {
        "id": str(app.id),
        "application_no": app.application_no,
        "child": app.child_name,
        "grade": app.grade,
        "source": app.source,
        "guardian": {"name": app.guardian_name, "initials": "".join(p[0] for p in app.guardian_name.split()[:1] + app.guardian_name.split()[-1:]).upper() if app.guardian_name else ""},
        "stage": app.stage,
        "date": (app.assessment_at.astimezone(tz) if slot_day else when.astimezone(tz)).date().isoformat(),
        "date_is_slot": bool(slot_day),
        "tag": _tag(app, tz),
        "approval_id": str(pending[app.id]) if app.id in pending else None,
        "closed": app.closed,
        "student_id": str(app.student_id) if app.student_id else None,
    }


def _filtered(params, year: str):
    qs = Application.objects.filter(academic_year=year)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(child_name__icontains=q) | Q(guardian_name__icontains=q) | Q(application_no__icontains=q) | Q(guardian_phone__contains=q))
    if params.get("grade"):
        grades = params["grade"].split(",")
        qs = qs.filter(grade__in=grades)
    if params.get("source"):
        qs = qs.filter(source=params["source"])
    return qs


def funnel(apps: list) -> dict:
    idx = {a.id: STAGES.index(a.stage) for a in apps}
    enquiries = len(apps)
    applications = sum(1 for a in apps if idx[a.id] >= 1)
    assessed = sum(1 for a in apps if idx[a.id] >= 3 or (a.stage == "assessment" and a.assessment_score is not None))
    offers = sum(1 for a in apps if idx[a.id] >= 4)
    admitted = sum(1 for a in apps if a.stage == "admitted")
    return {
        "enquiries": enquiries,
        "still_open": sum(1 for a in apps if a.stage == "enquiry" and not a.closed),
        "applications": applications,
        "application_rate": _pct(applications, enquiries),
        "assessed": assessed,
        "assessed_rate": _pct(assessed, applications),
        "offers": offers,
        "offer_rate": _pct(offers, assessed),
        "admitted": admitted,
        "accepted_rate": _pct(admitted, offers),
    }


def seats(cycle: AdmissionCycle, apps: list) -> dict:
    admitted = Counter(a.grade for a in apps if a.stage == "admitted")
    rows = []
    for plan in SeatPlan.objects.filter(cycle=cycle):
        filled = sum(admitted.get(g, 0) for g in plan.grades)
        rows.append({"id": str(plan.id), "label": plan.label, "grades": plan.grades, "seats": plan.seats, "admitted": filled, "open": max(0, plan.seats - filled)})
    return {
        "rows": rows,
        "seats": sum(r["seats"] for r in rows),
        "admitted": sum(r["admitted"] for r in rows),
        "open": sum(r["open"] for r in rows),
        "offers_outstanding": sum(1 for a in apps if a.stage == "offer" and not a.closed),
    }


class AdmissionsView(SchoolAPIView):
    """GET the page: funnel, sources, key dates, seats and the board (``?q=&grade=&source=`` filter the board).
    POST records a new enquiry."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        tz = school_tz(school)
        today = school_today(school)
        cycle = current_cycle()
        if cycle is None:
            return Response({"cycle": None})
        everything = list(Application.objects.filter(academic_year=cycle.academic_year))
        board = [a for a in _filtered(request.query_params, cycle.academic_year).filter(closed=False)]
        board.sort(key=lambda a: a.stage_changed_at or a.created_at, reverse=True)
        pending = _pending_requests(board)
        columns = []
        for stage in STAGES:
            items = [card(a, tz, pending) for a in board if a.stage == stage]
            if stage == "documents":
                # What's waiting on the principal comes first.
                items.sort(key=lambda c: (c["approval_id"] is None, c["tag"]["kind"] != "verified"))
            columns.append({"stage": stage, "count": len(items), "items": items})
        sources = Counter(a.source for a in everything)
        upcoming_slots = Counter(a.assessment_at.astimezone(tz).date() for a in everything if a.assessment_at and a.assessment_score is None and not a.closed and a.assessment_at.astimezone(tz).date() >= today)
        next_day = min(upcoming_slots) if upcoming_slots else None
        rounds = sorted((r for r in cycle.offer_rounds if r.get("on", "") >= today.isoformat()), key=lambda r: r["on"])
        updated = max((a.updated_at for a in everything), default=None)
        return Response(
            {
                "cycle": {
                    "academic_year": cycle.academic_year,
                    "enquiries_open_on": cycle.enquiries_open_on.isoformat(),
                    "applications_close_on": cycle.applications_close_on.isoformat(),
                    "session_starts_on": cycle.session_starts_on.isoformat(),
                },
                "awaiting_approval": ApprovalRequest.objects.filter(kind="admission", status=ApprovalRequest.Status.PENDING).count(),
                "updated_at": updated.isoformat() if updated else None,
                "funnel": funnel(everything),
                "sources": [{"source": s, "count": sources.get(s, 0), "percent": _pct(sources.get(s, 0), len(everything))} for s in Application.Source.values],
                "dates": {
                    "next_assessment": {"date": next_day.isoformat(), "children": upcoming_slots[next_day]} if next_day else None,
                    "next_offer_round": rounds[0] if rounds else None,
                    "applications_close_on": cycle.applications_close_on.isoformat(),
                },
                "seats": seats(cycle, everything),
                "columns": columns,
                "facets": {"grades": sorted({a.grade for a in everything}, key=_grade_key), "sources": list(Application.Source.values)},
                "classes": [
                    {"id": str(g.id), "label": g.short_label, "grade": g.grade}
                    for g in sorted(ClassGroup.objects.filter(academic_year__is_current=True), key=lambda g: (_grade_key(g.grade), g.section))
                ],
            }
        )

    def post(self, request):
        cycle = current_cycle()
        if cycle is None:
            raise ValidationError({"cycle": "Admissions aren't open."})
        data = request.data
        errors = {}
        child = " ".join(str(data.get("child_name") or "").split())
        if len(child) < 2:
            errors["child_name"] = "Enter the child's name."
        grade = str(data.get("grade") or "")
        if grade not in GRADES:
            errors["grade"] = "Choose the grade the child is applying for."
        source = str(data.get("source") or "walk_in")
        if source not in Application.Source.values:
            errors["source"] = "Choose where the enquiry came from."
        guardian = " ".join(str(data.get("guardian_name") or "").split())
        if len(guardian) < 2:
            errors["guardian_name"] = "Enter the parent's name."
        try:
            phone = normalize_phone(data.get("guardian_phone"))
        except ValueError as exc:
            errors["guardian_phone"] = str(exc)
            phone = ""
        if errors:
            raise ValidationError(errors)
        today = school_today(request.school)
        with transaction.atomic():
            last = Application.objects.filter(academic_year=cycle.academic_year).aggregate(m=Max("application_no"))["m"] or "APP-27-0000"
            number = int(last.rsplit("-", 1)[1]) + 1
            prefix = last.rsplit("-", 1)[0]
            app = Application.objects.create(
                application_no=f"{prefix}-{number:04d}",
                child_name=child,
                grade=grade,
                academic_year=cycle.academic_year,
                guardian_name=guardian,
                guardian_phone=phone,
                source=source,
                stage="enquiry",
                stage_changed_at=timezone.now(),
                enquired_on=today,
                follow_up="call_back",
                follow_up_on=today,
            )
            ApplicationEvent.objects.create(application=app, action="created", to_stage="enquiry", actor=request.user, note=app.get_source_display())
        return Response(card(app, school_tz(request.school), {}), status=201)


def _app(app_id) -> Application:
    try:
        uuid.UUID(str(app_id))
    except ValueError as exc:
        raise Http404 from exc
    app = Application.objects.filter(id=app_id).first()
    if app is None:
        raise Http404
    return app


def detail(app: Application, tz) -> dict:
    pending = _pending_requests([app])
    return {
        **card(app, tz, pending),
        "guardian_phone": app.guardian_phone,
        "academic_year": app.academic_year,
        "enquired_on": app.enquired_on.isoformat() if app.enquired_on else None,
        "follow_up": app.follow_up,
        "follow_up_on": app.follow_up_on.isoformat() if app.follow_up_on else None,
        "form_fee_paid": app.form_fee_paid,
        "documents_verified": app.documents_verified,
        "documents_pending": app.documents_pending,
        "assessment_at": app.assessment_at.astimezone(tz).isoformat() if app.assessment_at else None,
        "assessment_score": app.assessment_score,
        "offer_made_on": app.offer_made_on.isoformat() if app.offer_made_on else None,
        "offer_reply_by": app.offer_reply_by.isoformat() if app.offer_reply_by else None,
        "offer_accepted_on": app.offer_accepted_on.isoformat() if app.offer_accepted_on else None,
        "fee_due_on": app.fee_due_on.isoformat() if app.fee_due_on else None,
        "fee_receipt_no": app.fee_receipt_no,
        "admitted_on": app.admitted_on.isoformat() if app.admitted_on else None,
        "closed_reason": app.closed_reason,
        "sibling": {"id": str(app.sibling_id), "name": app.sibling.full_name, "class": app.sibling.class_group.short_label} if app.sibling_id else None,
        "events": [
            {"id": str(e.id), "action": e.action, "from_stage": e.from_stage, "to_stage": e.to_stage, "note": e.note, "actor": e.actor.full_name if e.actor else None, "at": e.created_at.isoformat()}
            for e in app.events.select_related("actor").order_by("-created_at")
        ],
    }


class ApplicationView(SchoolAPIView):
    """GET one application with its history. PATCH the office's details (follow-up, slot, score, documents, offer reply)."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, app_id):
        return Response(detail(_app(app_id), school_tz(request.school)))

    def patch(self, request, app_id):
        app = _app(app_id)
        tz = school_tz(request.school)
        data = request.data
        changed, events = [], []
        if "follow_up" in data:
            value = str(data.get("follow_up") or "")
            if value not in Application.FollowUp.values:
                raise ValidationError({"follow_up": "Choose call back, tour or prospectus."})
            app.follow_up = value
            changed.append("follow_up")
        if "follow_up_on" in data:
            app.follow_up_on = _date(data.get("follow_up_on"), "follow_up_on")
            changed.append("follow_up_on")
        if "form_fee_paid" in data:
            app.form_fee_paid = bool(data.get("form_fee_paid"))
            changed.append("form_fee_paid")
        if "documents_pending" in data:
            app.documents_pending = str(data.get("documents_pending") or "")[:80]
            changed.append("documents_pending")
        if "assessment_at" in data:
            raw = data.get("assessment_at")
            if raw:
                try:
                    moment = datetime.fromisoformat(str(raw))
                except ValueError as exc:
                    raise ValidationError({"assessment_at": "Use a date and time like 2026-10-01T10:00."}) from exc
                app.assessment_at = moment if moment.tzinfo else moment.replace(tzinfo=tz)
                events.append(("assessment", f"{app.assessment_at.astimezone(tz):%a %d %b · %I:%M %p}"))
            else:
                app.assessment_at = None
            changed.append("assessment_at")
        if "assessment_score" in data:
            raw = data.get("assessment_score")
            if raw in (None, ""):
                app.assessment_score = None
            else:
                try:
                    score = int(raw)
                except (TypeError, ValueError) as exc:
                    raise ValidationError({"assessment_score": "Enter the score as a number."}) from exc
                if not 0 <= score <= app.assessment_out_of:
                    raise ValidationError({"assessment_score": f"The score is out of {app.assessment_out_of}."})
                app.assessment_score = score
                events.append(("scored", f"{score}/{app.assessment_out_of}"))
            changed.append("assessment_score")
        if "offer_accepted" in data:
            if app.stage != "offer":
                raise ValidationError({"offer_accepted": "Only an offer can be accepted."})
            app.offer_accepted_on = school_today(request.school) if data.get("offer_accepted") else None
            if data.get("fee_due_on"):
                app.fee_due_on = _date(data.get("fee_due_on"), "fee_due_on")
            changed += ["offer_accepted_on", "fee_due_on"]
            if app.offer_accepted_on:
                events.append(("note", "Offer accepted"))
        if not changed:
            raise ValidationError({"detail": "Nothing to change."})
        with transaction.atomic():
            app.save(update_fields=[*changed, "updated_at"])
            for action, note in events:
                ApplicationEvent.objects.create(application=app, action=action, actor=request.user, note=note)
        return Response(detail(app, tz))


def _date(raw, field) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValidationError({field: "Use a date like 2026-10-01."}) from exc


class ApplicationMoveView(SchoolAPIView):
    """Move a card to another stage. Offers go through the approvals engine and admitting has its own step."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, app_id):
        app = _app(app_id)
        to = str(request.data.get("to_stage") or "")
        if to not in STAGES:
            raise ValidationError({"to_stage": "Choose a stage."})
        if app.closed:
            raise ValidationError({"to_stage": "This application is closed. Reopen it first."})
        if to == app.stage:
            raise ValidationError({"to_stage": "It is already in that stage."})
        now_i, to_i = STAGES.index(app.stage), STAGES.index(to)
        if app.stage in ("offer", "admitted"):
            raise ValidationError({"to_stage": "Offers and admissions can't be moved back from the board."})
        if to == "offer":
            raise ValidationError({"to_stage": "An offer needs your approval: approve it from the Documents column."})
        if to == "admitted":
            raise ValidationError({"to_stage": "Admit the child from the Offer column once the fee is paid."})
        if to_i > now_i + 1:
            raise ValidationError({"to_stage": "Move one stage at a time."})
        if to == "documents" and app.assessment_score is None:
            raise ValidationError({"to_stage": "Record the assessment score first."})
        if to == "assessment" and app.stage == "application" and not app.form_fee_paid:
            raise ValidationError({"to_stage": "The application form fee isn't paid yet."})
        with transaction.atomic():
            ApplicationEvent.objects.create(application=app, action="moved", from_stage=app.stage, to_stage=to, actor=request.user, note=str(request.data.get("note") or "")[:300])
            app.stage = to
            app.stage_changed_at = timezone.now()
            if to == "application":
                app.follow_up, app.follow_up_on = "", None
            app.save(update_fields=["stage", "stage_changed_at", "follow_up", "follow_up_on", "updated_at"])
        return Response(detail(app, school_tz(request.school)))


class ApplicationVerifyView(SchoolAPIView):
    """The office has checked every document: mark it verified and send it to the principal's in-tray."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, app_id):
        from apps.approvals.services import open_request, request_for

        app = _app(app_id)
        if app.stage != "documents":
            raise ValidationError({"stage": "Only applications in Documents can be verified."})
        with transaction.atomic():
            app.documents_verified, app.documents_pending = True, ""
            app.interaction_on = app.interaction_on or school_today(request.school)
            app.save(update_fields=["documents_verified", "documents_pending", "interaction_on", "updated_at"])
            ApplicationEvent.objects.create(application=app, action="verified", actor=request.user)
            existing = request_for(app)
            if not existing or existing.status != ApprovalRequest.Status.PENDING:
                open_request(kind="admission", target=app, requested_by=request.user, summary=f"Grade {app.grade} · {app.academic_year} · {app.application_no}")
        return Response(detail(app, school_tz(request.school)))


class ApplicationDecideView(SchoolAPIView):
    """Approve (make the offer) or decline, through the approvals engine. Decisions are audited."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, app_id):
        from apps.approvals.services import decide, request_for

        app = _app(app_id)
        decision = str(request.data.get("decision") or "")
        if decision not in ("approve", "decline"):
            raise ValidationError({"decision": "Use approve or decline."})
        req = request_for(app)
        if req is None or req.status != ApprovalRequest.Status.PENDING:
            raise ValidationError({"decision": "There's no decision waiting for this application."})
        note = str(request.data.get("note") or "")
        device = (request.META.get("HTTP_USER_AGENT") or "")[:200]
        with transaction.atomic():
            decide(req, decision, request.user, note=note, device=device)
            app.refresh_from_db()
            if decision == "approve":
                today = school_today(request.school)
                if not app.offer_reply_by:
                    app.offer_reply_by = today + timedelta(days=7)
                    app.save(update_fields=["offer_reply_by", "updated_at"])
                ApplicationEvent.objects.create(application=app, action="approved", from_stage="documents", to_stage="offer", actor=request.user, note=note[:300])
            else:
                ApplicationEvent.objects.create(application=app, action="declined", actor=request.user, note=note[:300])
        audit(request, f"approval.{decision}", target=req, module="admissions", summary=f"{app.child_name} · Grade {app.grade} · {app.application_no}")
        return Response(detail(app, school_tz(request.school)))


class ApplicationAdmitView(SchoolAPIView):
    """The fee is paid: admit the child. Creates the Student (active from the new session) and links the family."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, app_id):
        from apps.admissions.services import create_admitted_student

        app = _app(app_id)
        if app.stage != "offer" or app.closed:
            raise ValidationError({"stage": "Only an accepted offer can be admitted."})
        receipt = str(request.data.get("receipt_no") or "").strip()
        if not receipt:
            raise ValidationError({"receipt_no": "Enter the admission-fee receipt number."})
        group = None
        if request.data.get("class_id"):
            try:
                group = ClassGroup.objects.filter(id=uuid.UUID(str(request.data["class_id"]))).first()
            except ValueError:
                group = None
            if group is None:
                raise ValidationError({"class_id": "Choose a class."})
        today = school_today(request.school)
        with transaction.atomic():
            app.offer_accepted_on = app.offer_accepted_on or today
            app.fee_receipt_no = receipt[:40]
            app.admitted_on = today
            app.stage = "admitted"
            app.stage_changed_at = timezone.now()
            app.save(update_fields=["offer_accepted_on", "fee_receipt_no", "admitted_on", "stage", "stage_changed_at", "updated_at"])
            student = create_admitted_student(app, request.school, class_group=group)
            ApplicationEvent.objects.create(application=app, action="admitted", from_stage="offer", to_stage="admitted", actor=request.user, note=f"{student.admission_no} · receipt {receipt}")
        audit(request, "admissions.admit", target=app, summary=f"{app.child_name} admitted · {student.admission_no}")
        return Response(detail(app, school_tz(request.school)), status=201)


class ApplicationCloseView(SchoolAPIView):
    """Close (the family chose elsewhere, no response …) or reopen an application."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, app_id):
        app = _app(app_id)
        reopen = bool(request.data.get("reopen"))
        if app.stage == "admitted":
            raise ValidationError({"stage": "An admitted child can't be closed here."})
        if reopen:
            if not app.closed:
                raise ValidationError({"closed": "It is already open."})
            app.closed, app.closed_reason = False, ""
            note = "Reopened"
        else:
            reason = str(request.data.get("reason") or "").strip()
            if not reason:
                raise ValidationError({"reason": "Say why it is being closed."})
            if app.closed:
                raise ValidationError({"closed": "It is already closed."})
            app.closed, app.closed_reason = True, reason[:120]
            note = reason
        with transaction.atomic():
            app.save(update_fields=["closed", "closed_reason", "updated_at"])
            ApplicationEvent.objects.create(application=app, action="closed" if app.closed else "note", actor=request.user, note=note[:300])
            if app.closed:
                from apps.approvals.services import withdraw

                withdraw(app, request.user)
        return Response(detail(app, school_tz(request.school)))


urlpatterns = [
    path("console/admissions", AdmissionsView.as_view()),
    path("console/admissions/<str:app_id>", ApplicationView.as_view()),
    path("console/admissions/<str:app_id>/move", ApplicationMoveView.as_view()),
    path("console/admissions/<str:app_id>/verify", ApplicationVerifyView.as_view()),
    path("console/admissions/<str:app_id>/decide", ApplicationDecideView.as_view()),
    path("console/admissions/<str:app_id>/admit", ApplicationAdmitView.as_view()),
    path("console/admissions/<str:app_id>/close", ApplicationCloseView.as_view()),
]
