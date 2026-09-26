"""Console: approvals, the principal's web in-tray.

Reuses ``apps.approvals`` (payload, decide, undo) and adds what the wide page shows: SLA ages, the audit trail with
the HOD check, evidence files, today's decisions, the decision history and the SLA rules.
"""

from datetime import datetime, time, timedelta

from django.http import FileResponse, Http404
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.accounts.audit import audit
from apps.accounts.models import Membership, Role
from apps.approvals import services
from apps.approvals.models import ApprovalAttachment, ApprovalEvent, ApprovalRequest
from apps.approvals.sla import DEFAULT_SLA_HOURS, sla_hours, sla_state
from apps.approvals.views import urgency
from apps.core.api import SchoolAPIView
from apps.core.utils import school_now, school_today, school_tz

from .common import CONSOLE_ROLES

# The in-tray's sections, in the order the page shows them.
KINDS = ["marks", "leave", "refund", "admission", "attendance"]
DECIDED = [ApprovalRequest.Status.APPROVED, ApprovalRequest.Status.DECLINED, ApprovalRequest.Status.SENT_BACK]
SLA_CHOICES = (24, 48, 72, 96, 120)
VERBS = {"approve": "approve", "send_back": "send_back", "reject": "decline", "decline": "decline"}


def next_school_day(today):
    day = today + timedelta(days=1)
    return day + timedelta(days=1) if day.weekday() == 6 else day


def _titles(user_ids) -> dict:
    """Staff titles ("Science · HOD") for the people in a trail."""
    out = {}
    for m in Membership.objects.filter(user_id__in=[u for u in user_ids if u], is_active=True).exclude(role__in=[Role.PARENT, Role.STUDENT]):
        if m.title and str(m.user_id) not in out:
            out[str(m.user_id)] = m.title
    return out


def _file_size(f) -> int:
    try:
        return f.size
    except (OSError, ValueError):
        return 0


def _trail(req: ApprovalRequest, titles: dict) -> list[dict]:
    events = list(req.events.select_related("actor").all())
    out = [
        {
            "action": e.action,
            "at": e.created_at.isoformat(),
            "actor": {"id": str(e.actor_id), "name": e.actor.full_name, "initials": e.actor.initials, "title": titles.get(str(e.actor_id))} if e.actor else None,
            "note": e.note,
        }
        for e in events
    ]
    checker = getattr(req.target, "checked_by", None) if req.kind == ApprovalRequest.Kind.MARKS else None
    if checker and not any(e.action == ApprovalEvent.Action.CHECKED for e in events):
        # Checked before the approvals app recorded checks: we know who, not when.
        at = 1 if out and out[0]["action"] == ApprovalEvent.Action.SUBMITTED else 0
        out.insert(at, {"action": "checked", "at": None, "actor": {"id": str(checker.id), "name": checker.full_name, "initials": checker.initials, "title": titles.get(str(checker.id))}, "note": ""})
    return out


def _attachments(req: ApprovalRequest) -> list[dict]:
    files = [
        {
            "id": str(a.id),
            "name": a.name,
            "kind": a.kind,
            "pages": a.pages,
            "size": a.size or _file_size(a.file),
            "type": (a.file.name.rsplit(".", 1)[-1] if "." in a.file.name else "").upper(),
            "url": f"/console/approvals/{req.id}/files/{a.id}",
        }
        for a in req.attachments.all()
    ]
    certificate = getattr(req.target, "certificate", None) if req.kind == ApprovalRequest.Kind.LEAVE else None
    if certificate:
        name = req.target.certificate_name or "Certificate"
        files.append({"id": "certificate", "name": name.rsplit(".", 1)[0], "kind": "document", "pages": None, "size": _file_size(certificate), "type": "PDF" if certificate.name.lower().endswith(".pdf") else "", "url": f"/approvals/{req.id}/file"})
    return files


def item(req: ApprovalRequest, now, today) -> dict:
    """``services.payload`` plus what the console needs: SLA, trail, evidence and the requester's title."""
    data = services.payload(req)
    people = [req.requested_by_id, req.decided_by_id, *req.events.values_list("actor_id", flat=True)]
    checker = getattr(req.target, "checked_by_id", None) if req.kind == ApprovalRequest.Kind.MARKS else None
    titles = _titles([*people, checker])
    sla = sla_state(req, now)
    tz = school_tz(req.school)
    deadline = datetime.fromisoformat(sla["due_at"]).astimezone(tz).date()
    pending = req.status == ApprovalRequest.Status.PENDING
    data.update(
        {
            "sla": sla if pending else None,
            # The answer is needed today: the leave starts on the next school day, or the SLA runs out today.
            "decide_today": pending and not sla["past"] and ((req.due_on is not None and req.due_on <= next_school_day(today)) or deadline <= today),
            "requester_title": titles.get(str(req.requested_by_id)),
            "trail": _trail(req, titles),
            "attachments": _attachments(req),
        }
    )
    return data


def _get(request_id) -> ApprovalRequest:
    req = ApprovalRequest.objects.filter(id=request_id).select_related("requested_by", "decided_by", "school").first()
    if req is None:
        raise Http404
    return req


def rules(school) -> dict:
    stored = (((school.settings or {}).get("approvals") or {}).get("sla_hours")) or {}
    return {
        "sla_hours": {k: sla_hours(k, school) for k in KINDS},
        "default_hours": int(stored.get("default", DEFAULT_SLA_HOURS)),
        "choices": list(SLA_CHOICES),
        "undo_minutes": services.UNDO_MINUTES,
        "note_required": ["send_back", "reject"],
        "deciders": sorted({m.user.full_name for m in Membership.objects.filter(role=Role.PRINCIPAL, is_active=True).select_related("user")}),
    }


class ApprovalsView(SchoolAPIView):
    """The in-tray: every pending request with its SLA, plus what was decided today."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        now = timezone.now()
        today = school_today(school)
        pending = list(ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING).select_related("requested_by", "school"))
        pending.sort(key=urgency(today))
        items = [item(r, now, today) for r in pending]
        start = datetime.combine(today, time.min, tzinfo=school_tz(school))
        decided = list(ApprovalRequest.objects.filter(status__in=DECIDED, decided_at__gte=start).select_related("requested_by", "decided_by", "school").order_by("-decided_at"))
        tomorrow = next_school_day(today)
        urgent = [i["details"]["person"]["name"] for i in items if i["kind"] == "leave" and i["due_on"] and i["due_on"] <= tomorrow.isoformat()]
        return Response(
            {
                "now": school_now(school).isoformat(),
                "today": today.isoformat(),
                "total": len(items),
                "counts": {k: sum(1 for i in items if i["kind"] == k) for k in KINDS},
                "past_sla": sum(1 for i in items if i["sla"]["past"]),
                "sla_hours": sla_hours("default", school),
                "urgent": {"names": urgent, "day": tomorrow.isoformat()},
                "items": items,
                "decided_today": {"total": len(decided), "items": [item(r, now, today) for r in decided]},
                "signer": request.user.full_name,
            }
        )


class ApprovalDetailView(SchoolAPIView):
    """One request (pending or decided), for deep links and the history sheet."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, request_id):
        return Response(item(_get(request_id), timezone.now(), school_today(request.school)))


class ApprovalHistoryView(SchoolAPIView):
    """Decided requests, newest first. ``range`` = today | week | all, ``status`` and ``kind`` narrow it."""

    allowed_roles = CONSOLE_ROLES
    LIMIT = 60

    def get(self, request):
        school = request.school
        today = school_today(school)
        qs = ApprovalRequest.objects.filter(status__in=DECIDED, decided_at__isnull=False).select_related("requested_by", "decided_by", "school")
        span = request.query_params.get("range", "all")
        if span in ("today", "week"):
            first = today if span == "today" else today - timedelta(days=6)
            qs = qs.filter(decided_at__gte=datetime.combine(first, time.min, tzinfo=school_tz(school)))
        elif span != "all":
            raise ValidationError({"range": "Use today, week or all."})
        kind = request.query_params.get("kind")
        if kind:
            if kind not in KINDS:
                raise ValidationError({"kind": "Unknown kind."})
            qs = qs.filter(kind=kind)
        counts = {s: qs.filter(status=s).count() for s in DECIDED}
        status = request.query_params.get("status")
        if status:
            status = VERBS.get(status, status)
            status = {"approve": "approved", "decline": "declined", "send_back": "sent_back"}.get(status, status)
            if status not in DECIDED:
                raise ValidationError({"status": "Use approved, sent_back or declined."})
            qs = qs.filter(status=status)
        rows = list(qs.order_by("-decided_at")[: self.LIMIT])
        now = timezone.now()
        return Response({"total": sum(counts.values()), "counts": counts, "items": [item(r, now, today) for r in rows], "limit": self.LIMIT})


class ApprovalDecideView(SchoolAPIView):
    """Approve, send back or reject. Sending back and rejecting both need a note from the console."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request, request_id):
        decision = str(request.data.get("decision", ""))
        note = str(request.data.get("note", "")).strip()
        if decision not in VERBS:
            raise ValidationError({"decision": "Use approve, send_back or reject."})
        if decision != "approve" and not note:
            raise ValidationError({"note": "Add a note so they know what to fix." if decision == "send_back" else "Add a note to say why."})
        req = services.decide(_get(request_id), VERBS[decision], request.user, note=note, device=request.META.get("HTTP_USER_AGENT", "")[:200])
        verb = {"approve": "approve", "send_back": "send_back"}.get(decision, "reject")
        audit(request, f"approvals.{verb}", target=req, summary=f"{req.get_kind_display()} · {req.summary}"[:300], detail={"note": note, "kind": req.kind})
        return Response(item(req, timezone.now(), school_today(request.school)))


class ApprovalUndoView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def post(self, request, request_id):
        req = services.undo(_get(request_id), request.user, device=request.META.get("HTTP_USER_AGENT", "")[:200])
        audit(request, "approvals.undo", target=req, summary=f"{req.get_kind_display()} · {req.summary}"[:300])
        return Response(item(req, timezone.now(), school_today(request.school)))


class ApprovalFileView(SchoolAPIView):
    """Download one piece of evidence."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, request_id, file_id):
        doc = ApprovalAttachment.objects.filter(id=file_id, request_id=request_id).first()
        if doc is None or not doc.file:
            raise Http404
        audit(request, "approvals.download", target=doc.request, summary=doc.name)
        return FileResponse(doc.file.open("rb"), as_attachment=False, filename=doc.file.name.rsplit("/", 1)[-1])


class ApprovalRulesView(SchoolAPIView):
    """How long each kind may wait (the SLA), stored in ``school.settings["approvals"]``."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        return Response(rules(request.school))

    def put(self, request):
        given = request.data.get("sla_hours")
        if not isinstance(given, dict) or not given:
            raise ValidationError({"sla_hours": "Send the hours for each kind."})
        clean = {}
        for kind, hours in given.items():
            if kind not in KINDS:
                raise ValidationError({"sla_hours": f"Unknown kind: {kind}."})
            try:
                hours = int(hours)
            except (TypeError, ValueError):
                raise ValidationError({"sla_hours": "Hours must be whole numbers."}) from None
            if hours not in SLA_CHOICES:
                raise ValidationError({"sla_hours": f"Choose one of {', '.join(map(str, SLA_CHOICES))} hours."})
            clean[kind] = hours
        school = request.school
        school.refresh_from_db(fields=["settings"])
        settings = dict(school.settings or {})
        block = dict(settings.get("approvals") or {})
        block["sla_hours"] = {**(block.get("sla_hours") or {}), **clean}
        settings["approvals"] = block
        school.settings = settings
        school.save(update_fields=["settings"])
        audit(request, "approvals.rules", target=school, summary=", ".join(f"{k} {v}h" for k, v in clean.items()), detail={"sla_hours": clean})
        return Response(rules(school))


urlpatterns = [
    path("console/approvals", ApprovalsView.as_view()),
    path("console/approvals/history", ApprovalHistoryView.as_view()),
    path("console/approvals/rules", ApprovalRulesView.as_view()),
    path("console/approvals/<uuid:request_id>", ApprovalDetailView.as_view()),
    path("console/approvals/<uuid:request_id>/decide", ApprovalDecideView.as_view()),
    path("console/approvals/<uuid:request_id>/undo", ApprovalUndoView.as_view()),
    path("console/approvals/<uuid:request_id>/files/<uuid:file_id>", ApprovalFileView.as_view()),
]
