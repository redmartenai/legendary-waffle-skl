"""Open, decide, undo and withdraw approval requests. Every step is written to the audit trail."""

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import MANAGEMENT_ROLES, Membership
from apps.notifications.models import Category
from apps.notifications.services import notify

from .handlers import HANDLERS
from .models import ApprovalEvent, ApprovalRequest

UNDO_MINUTES = 10


def open_request(*, kind: str, target, requested_by, summary: str = "", due_on=None, notify_principal: bool = True) -> ApprovalRequest:
    req = ApprovalRequest.objects.create(
        kind=kind,
        target_type=ContentType.objects.get_for_model(target),
        target_id=target.pk,
        requested_by=requested_by,
        summary=summary[:200],
        due_on=due_on,
    )
    ApprovalEvent.objects.create(request=req, actor=requested_by, action=ApprovalEvent.Action.SUBMITTED)
    checker = getattr(target, "checked_by", None)
    if checker is not None:
        # Filed already checked (a marks correction the HOD has seen): the check is part of the trail.
        record_check(req, checker)
    if notify_principal:
        principals = [m.user for m in Membership.objects.filter(role__in=MANAGEMENT_ROLES, is_active=True).select_related("user")]
        who = requested_by.full_name if requested_by else "Someone"
        notify(
            principals,
            school=req.school,
            category=Category.GENERAL,
            title=f"{ApprovalRequest.Kind(kind).label} request from {who}",
            body=summary[:180],
            data={"type": "approval", "approval_id": str(req.id)},
        )
    return req


def record_check(req: ApprovalRequest, checker, *, note: str = "", when=None) -> ApprovalEvent:
    """Someone other than the requester (an HOD) checked the request before it reached the principal."""
    event = ApprovalEvent.objects.create(request=req, actor=checker, action=ApprovalEvent.Action.CHECKED, note=note[:300])
    if when is not None:
        ApprovalEvent.objects.filter(pk=event.pk).update(created_at=when)
        event.created_at = when
    return event


def request_for(target) -> ApprovalRequest | None:
    return ApprovalRequest.objects.filter(target_type=ContentType.objects.get_for_model(target), target_id=target.pk).order_by("-created_at").first()


def decide(req: ApprovalRequest, action: str, actor, *, note: str = "", device: str = "") -> ApprovalRequest:
    if req.status != ApprovalRequest.Status.PENDING:
        raise ValidationError({"status": "This request has already been decided."})
    handler = HANDLERS[req.kind]
    with transaction.atomic():
        req.decision_note = note[:300]
        if action == "approve":
            handler.apply(req, req.target, actor)
            req.status = ApprovalRequest.Status.APPROVED
        elif action in ("decline", "send_back"):
            if action == "send_back" and not note.strip():
                raise ValidationError({"note": "Say what needs fixing."})
            handler.decline(req, req.target, actor)
            req.status = ApprovalRequest.Status.DECLINED if action == "decline" else ApprovalRequest.Status.SENT_BACK
        else:
            raise ValidationError({"decision": "Use approve, decline or send_back."})
        req.decided_by = actor
        req.decided_at = timezone.now()
        req.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
        ApprovalEvent.objects.create(request=req, actor=actor, action=req.status, note=note[:300], device=device[:200])
    return req


def undo(req: ApprovalRequest, actor, *, device: str = "") -> ApprovalRequest:
    if req.status in (ApprovalRequest.Status.PENDING, ApprovalRequest.Status.WITHDRAWN) or not req.decided_at:
        raise ValidationError({"status": "There's nothing to undo."})
    if timezone.now() - req.decided_at > timedelta(minutes=UNDO_MINUTES):
        raise ValidationError({"status": f"Decisions can be undone for {UNDO_MINUTES} minutes."})
    with transaction.atomic():
        HANDLERS[req.kind].revert(req, req.target, actor)
        req.status = ApprovalRequest.Status.PENDING
        req.decided_by = None
        req.decided_at = None
        req.decision_note = ""
        req.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
        ApprovalEvent.objects.create(request=req, actor=actor, action=ApprovalEvent.Action.UNDONE, device=device[:200])
    return req


def withdraw(target, actor) -> None:
    req = request_for(target)
    if req and req.status == ApprovalRequest.Status.PENDING:
        req.status = ApprovalRequest.Status.WITHDRAWN
        req.save(update_fields=["status", "updated_at"])
        ApprovalEvent.objects.create(request=req, actor=actor, action=ApprovalEvent.Action.WITHDRAWN)


def payload(req: ApprovalRequest) -> dict:
    undo_until = req.decided_at + timedelta(minutes=UNDO_MINUTES) if req.decided_at else None
    return {
        "id": str(req.id),
        "kind": req.kind,
        "status": req.status,
        "summary": req.summary,
        "due_on": req.due_on.isoformat() if req.due_on else None,
        "created_at": req.created_at.isoformat(),
        "requested_by": {"id": str(req.requested_by_id), "name": req.requested_by.full_name, "initials": req.requested_by.initials} if req.requested_by else None,
        "decided_by": req.decided_by.full_name if req.decided_by else None,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "decision_note": req.decision_note,
        "undo_until": undo_until.isoformat() if undo_until and undo_until > timezone.now() else None,
        "details": HANDLERS[req.kind].details(req, req.target),
    }
