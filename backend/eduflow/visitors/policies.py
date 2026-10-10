"""Visits: security and the office see every visit (school scope); a host or the member who registered a visit
sees it (self scope)."""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource

from .models import Visit

visits: ScopedResource[Visit] = ScopedResource("visit", Visit)

visits.rule(DataScope.SELF)(lambda actor: Q(host=actor.membership) | Q(registered_by=actor.membership))
