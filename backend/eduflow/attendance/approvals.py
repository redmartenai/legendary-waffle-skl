"""Attendance corrections in the central approvals queue (eduflow.approvals)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eduflow.approvals.registry import ApprovalItem, Provider, registry
from eduflow.authz.grants import Actor

from . import policies, services
from .models import AttendanceCorrection, CorrectionStatus


def _pending(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        AttendanceCorrection.objects.for_school(actor.school)
        .filter(status=CorrectionStatus.PENDING)
        .select_related("record__student", "record__section", "requested_by__user")
    )
    for c in rows:
        yield ApprovalItem(
            kind="attendance_correction",
            id=c.pk,
            title=f"Attendance correction: {c.old_status} -> {c.new_status}",
            detail=c.reason,
            subject=f"{c.record.student.full_name} · {c.record.section.code} · {c.record.date}",
            requested_by=c.requested_by.user.full_name,
            requested_at=c.created_at,
        )


def _decide(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    correction = policies.corrections.get(actor, "attendance.approve", pk)
    if decision == "approve":
        return services.approve_correction(actor, correction, note=note)
    return services.decline_correction(actor, correction, note=note)


def register() -> None:
    registry.register(
        Provider("attendance_correction", "Attendance correction", "attendance.approve", _pending, _decide)
    )
