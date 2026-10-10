"""Hostel outpasses in the central approvals queue."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eduflow.approvals.registry import ApprovalItem, Provider, registry
from eduflow.authz.grants import Actor

from . import policies, services
from .models import Outpass, OutpassStatus

PERMISSION = "hostel.approve"


def _pending(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        Outpass.objects.for_school(actor.school)
        .filter(status=OutpassStatus.PENDING)
        .select_related("student", "requested_by__user")
    )
    for o in rows:
        yield ApprovalItem(
            kind="outpass",
            id=o.pk,
            title=f"Outpass: {o.leave_at:%d %b %H:%M} to {o.return_by:%d %b %H:%M}",
            detail=o.reason,
            subject=o.student.full_name,
            requested_by=o.requested_by.user.full_name,
            requested_at=o.created_at,
        )


def _decide(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    return services.decide_outpass(actor, policies.outpasses.get(actor, PERMISSION, pk), decision, note)


def register() -> None:
    registry.register(Provider("outpass", "Hostel outpass", PERMISSION, _pending, _decide))
