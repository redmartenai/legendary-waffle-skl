"""Remarks and incidents follow their student (people.scoping). Families (child, self) see only the records
marked ``visible_to_family``; teachers see every record about the students they teach."""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import children, own_student, taught_students

from .models import Incident, Remark

remarks: ScopedResource[Remark] = ScopedResource("remark", Remark)
incidents: ScopedResource[Incident] = ScopedResource("incident", Incident)

FAMILY = Q(visible_to_family=True)


def _register(resource: ScopedResource[Any]) -> None:
    resource.rule(DataScope.SECTION)(lambda actor: taught_students("student__", actor))
    resource.rule(DataScope.CHILD)(lambda actor: children("student__", actor) & FAMILY)
    resource.rule(DataScope.SELF)(lambda actor: own_student("student__", actor) & FAMILY)


_register(remarks)
_register(incidents)
