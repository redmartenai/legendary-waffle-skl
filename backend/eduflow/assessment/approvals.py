"""Mark sheets and marks corrections in the central approvals queue."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eduflow.approvals.registry import ApprovalItem, Provider, registry
from eduflow.authz.grants import Actor

from . import policies, services
from .models import CorrectionStatus, MarkCorrection, MarkSheet, SheetStatus

PERMISSION = "assessment.approve"


def _sheets(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        MarkSheet.objects.for_school(actor.school)
        .filter(status=SheetStatus.SUBMITTED)
        .select_related("exam", "section", "subject", "submitted_by__user")
    )
    for sheet in rows:
        yield ApprovalItem(
            kind="mark_sheet",
            id=sheet.pk,
            title=f"Marks: {sheet.exam.name} · {sheet.subject.name}",
            subject=sheet.section.code,
            requested_by=sheet.submitted_by.user.full_name if sheet.submitted_by else "",
            requested_at=sheet.submitted_at or sheet.updated_at,
        )


def _decide_sheet(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    return services.decide_sheet(actor, policies.sheets.get(actor, PERMISSION, pk), decision, note)


def _corrections(actor: Actor) -> Iterable[ApprovalItem]:
    rows = (
        MarkCorrection.objects.for_school(actor.school)
        .filter(status=CorrectionStatus.PENDING)
        .select_related("mark__student", "mark__sheet__exam", "mark__sheet__subject", "requested_by__user")
    )
    for c in rows:
        old = "absent" if c.old_absent else c.old_marks
        new = "absent" if c.new_absent else c.new_marks
        yield ApprovalItem(
            kind="marks_correction",
            id=c.pk,
            title=f"Marks correction: {old} -> {new}",
            detail=c.reason,
            subject=f"{c.mark.student.full_name} · {c.mark.sheet.exam.name} · {c.mark.sheet.subject.name}",
            requested_by=c.requested_by.user.full_name,
            requested_at=c.created_at,
        )


def _decide_correction(actor: Actor, pk: Any, decision: str, note: str) -> Any:
    return services.decide_correction(actor, policies.corrections.get(actor, PERMISSION, pk), decision, note)


def register() -> None:
    registry.register(Provider("mark_sheet", "Mark sheet", PERMISSION, _sheets, _decide_sheet))
    registry.register(
        Provider("marks_correction", "Marks correction", PERMISSION, _corrections, _decide_correction)
    )
