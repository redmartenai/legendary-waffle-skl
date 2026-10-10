"""Purchase requisitions in the central approvals queue (prototype approval kind ``expense``)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eduflow.approvals.registry import ApprovalItem, Provider, registry
from eduflow.authz.grants import Actor

from . import policies, services
from .models import Requisition, RequisitionStatus

PERMISSION = "procurement.approve"


def _pending(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        Requisition.objects.for_school(actor.school)
        .filter(status=RequisitionStatus.PENDING)
        .select_related("requested_by__user")
    )
    for r in rows:
        yield ApprovalItem(
            kind="expense",
            id=r.pk,
            title=f"Purchase: {r.title}",
            detail=r.justification,
            requested_by=r.requested_by.user.full_name,
            requested_at=r.created_at,
            amount=r.estimated_total,
        )


def _decide(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    return services.decide_requisition(
        actor, policies.requisitions.get(actor, PERMISSION, pk), decision, note
    )


def register() -> None:
    registry.register(Provider("expense", "Purchase requisition", PERMISSION, _pending, _decide))
