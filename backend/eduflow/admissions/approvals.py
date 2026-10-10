"""Admission offers in the central approvals queue."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eduflow.approvals.registry import ApprovalItem, Provider, registry
from eduflow.authz.grants import Actor

from . import policies, services
from .models import Application, OfferDecision, Stage


def _pending(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        Application.objects.for_school(actor.school)
        .filter(stage=Stage.OFFER, offer_decision=OfferDecision.PENDING)
        .select_related("grade", "offer_requested_by__user")
    )
    for app in rows:
        yield ApprovalItem(
            kind="admission",
            id=app.pk,
            title=f"Admission offer: {app.child_name}",
            detail=app.notes,
            subject=f"{app.grade.name} · {app.parent_name}",
            requested_by=app.offer_requested_by.user.full_name if app.offer_requested_by else "",
            requested_at=app.offer_requested_at or app.updated_at,
            amount=app.fee_quoted,
        )


def _decide(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    return services.decide_offer(
        actor, policies.applications.get(actor, "admission.approve", pk), decision, note
    )


def register() -> None:
    registry.register(Provider("admission", "Admission offer", "admission.approve", _pending, _decide))
