"""Leave requests in the central approvals queue."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eduflow.approvals.registry import ApprovalItem, Provider, registry
from eduflow.authz.grants import Actor

from . import policies, services
from .models import LeaveRequest, LeaveStatus

PERMISSION = "leave.approve"


def _pending(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        LeaveRequest.objects.for_school(actor.school)
        .filter(status=LeaveStatus.PENDING)
        .select_related("staff__membership__user", "leave_type")
    )
    for r in rows:
        yield ApprovalItem(
            kind="leave",
            id=r.pk,
            title=f"{r.leave_type.name}: {r.start_date} to {r.end_date} ({r.days} day(s))",
            detail=r.reason,
            subject=r.staff.membership.user.full_name,
            requested_by=r.staff.membership.user.full_name,
            requested_at=r.created_at,
        )


def _decide(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    return services.decide_leave(actor, policies.leave_requests.get(actor, PERMISSION, pk), decision, note)


def register() -> None:
    registry.register(Provider("leave", "Leave", PERMISSION, _pending, _decide))
