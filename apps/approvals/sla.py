"""How long each kind of request may wait for a decision (the "two-day promise"), and where a request stands against it.

The approvals console owns the policy (it may later be stored per school); other pages call ``sla_state``.
"""

from datetime import timedelta

DEFAULT_SLA_HOURS = 48


def sla_hours(kind: str, school=None) -> int:
    rules = (((getattr(school, "settings", None) or {}).get("approvals") or {}).get("sla_hours") or {}) if school else {}
    return int(rules.get(kind, rules.get("default", DEFAULT_SLA_HOURS)))


def sla_state(req, now) -> dict:
    """Age of a pending request and whether it is past its SLA (hours are whole and rounded down)."""
    hours = sla_hours(req.kind, req.school)
    age = now - req.created_at
    due_at = req.created_at + timedelta(hours=hours)
    past_by = now - due_at
    return {
        "age_hours": int(age.total_seconds() // 3600),
        "sla_hours": hours,
        "due_at": due_at.isoformat(),
        "past": past_by.total_seconds() > 0,
        "past_by_hours": max(0, int(past_by.total_seconds() // 3600)),
        "hours_left": max(0, int(-past_by.total_seconds() // 3600)),
    }
