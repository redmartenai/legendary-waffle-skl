"""Fee refunds in the central approvals queue."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eduflow.approvals.registry import ApprovalItem, Provider, registry
from eduflow.authz.grants import Actor

from . import policies, services
from .models import Refund, RefundStatus

PERMISSION = "fee.approve"


def _pending(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        Refund.objects.for_school(actor.school)
        .filter(status=RefundStatus.PENDING)
        .select_related("payment__student", "requested_by__user")
    )
    for r in rows:
        yield ApprovalItem(
            kind="refund",
            id=r.pk,
            title=f"Refund against receipt {r.payment.receipt_number}",
            detail=r.reason,
            subject=r.payment.student.full_name,
            requested_by=r.requested_by.user.full_name,
            requested_at=r.created_at,
            amount=r.amount,
        )


def _decide(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    return services.decide_refund(actor, policies.refunds.get(actor, PERMISSION, pk), decision, note)


def register() -> None:
    registry.register(Provider("refund", "Fee refund", PERMISSION, _pending, _decide))
