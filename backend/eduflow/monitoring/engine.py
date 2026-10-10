"""Evaluating the rules into alerts: deduplication, lifecycle, auto-resolution and escalation.

Runs per school (``tenancy.jobs`` "frequent", every 15 minutes by default) and on demand
(``POST /monitoring/evaluate``). Idempotent: running it twice in a row changes nothing but ``last_seen_at``.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from eduflow.audit import services as audit
from eduflow.authz.models import MembershipRole
from eduflow.notifications import services as notifications
from eduflow.people.models import StaffProfile
from eduflow.tenancy.models import Membership

from . import rules
from .models import Alert, AlertStatus

LEADERS = ("principal", "school_admin")


def _leaders(school: Any) -> list[Membership]:
    ids = MembershipRole.objects.filter(
        membership__school_id=school.pk, membership__is_active=True, role__key__in=LEADERS
    ).values_list("membership_id", flat=True)
    return list(Membership.objects.filter(pk__in=ids))


def _owners(school: Any, finding: rules.Finding) -> list[Membership]:
    staff = StaffProfile.objects.filter(school_id=school.pk, pk__in=finding.staff_ids).select_related(
        "membership"
    )
    return [s.membership for s in staff]


@transaction.atomic
def evaluate(school: Any, now: datetime.datetime | None = None) -> dict[str, int]:
    now = now or timezone.now()
    findings = rules.evaluate_rules(school, now)
    live = {
        a.rule: a
        for a in Alert.objects.select_for_update()
        .filter(school_id=school.pk)
        .exclude(status=AlertStatus.RESOLVED)
    }
    opened = resolved = escalated = 0
    for key, finding in findings.items():
        rule = rules.RULES[key]
        alert = live.get(key)
        if finding is None:
            if alert is not None:
                alert.status, alert.resolved_at, alert.auto_resolved = AlertStatus.RESOLVED, now, True
                alert.save()
                resolved += 1
            continue
        fields = {
            "title": rule.title,
            "domain": rule.domain,
            "severity": finding.severity or rule.severity,
            "headline": finding.headline[:300],
            "why": finding.why[:1000],
            "owner": rule.owner,
            "escalates_to": rule.escalates_to,
            "items": finding.items,
            "staff_ids": sorted(finding.staff_ids),
            "section_ids": sorted(finding.section_ids),
            "last_seen_at": now,
        }
        if alert is None:
            alert = Alert.objects.create(school_id=school.pk, rule=key, first_seen_at=now, **fields)
            opened += 1
            notifications.notify(
                school,
                _owners(school, finding) + _leaders(school),
                kind="alert",
                title=finding.headline[:200],
                body=finding.why[:300],
                link=("alert", alert.pk),
            )
        else:
            for name, value in fields.items():
                setattr(alert, name, value)
            alert.occurrences += 1
        due = rule.escalate_after_hours
        if (
            due is not None
            and alert.status == AlertStatus.OPEN
            and alert.escalated_at is None
            and now - alert.first_seen_at >= datetime.timedelta(hours=due)
        ):
            alert.escalated_at = now
            escalated += 1
            notifications.notify(
                school,
                _leaders(school),
                kind="alert",
                title=f"Escalated: {finding.headline[:180]}",
                body=f"Unacknowledged since {alert.first_seen_at:%d %b %H:%M}. Owner: {rule.owner}.",
                link=("alert", alert.pk),
            )
        alert.save()
    if opened or resolved or escalated:
        audit.record(
            "monitoring.evaluated",
            actor_id=None,
            school_id=school.pk,
            target_type="school",
            target_id=school.pk,
            metadata={"opened": opened, "resolved": resolved, "escalated": escalated},
        )
    return {
        "opened": opened,
        "resolved": resolved,
        "escalated": escalated,
        "live": len([f for f in findings.values() if f]),
    }
