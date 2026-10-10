"""Alerts: the principal and the office see every alert (school scope). A staff member (self scope) sees the
alerts with an item they own (their class, their marks, their messages); the API trims the items to theirs.
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource
from eduflow.people.models import StaffProfile

from .models import Alert

alerts: ScopedResource[Alert] = ScopedResource("alert", Alert)


def my_staff_id(actor: Actor) -> object | None:
    return StaffProfile.objects.filter(membership=actor.membership).values_list("pk", flat=True).first()


@alerts.rule(DataScope.SELF)
def _mine(actor: Actor) -> Q:
    staff_id = my_staff_id(actor)
    return Q(staff_ids__contains=[staff_id]) if staff_id else Q(pk__in=[])
