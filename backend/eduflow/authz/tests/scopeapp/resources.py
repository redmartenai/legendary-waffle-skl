"""How a domain module declares data-scope rules for its resource."""

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource

from .models import Student

students: ScopedResource[Student] = ScopedResource("student", Student)


@students.rule(DataScope.SECTION)
def _taught_sections(actor: Actor) -> Q:
    return Q(section__teachers__membership=actor.membership)


@students.rule(DataScope.ASSIGNED)
def _assigned(actor: Actor) -> Q:
    return Q(mentors__membership=actor.membership)


@students.rule(DataScope.CHILD)
def _children(actor: Actor) -> Q:
    return Q(guardians__membership=actor.membership)


@students.rule(DataScope.SELF)
def _self(actor: Actor) -> Q:
    return Q(user=actor.user)


@students.rule(DataScope.OWN)
def _created(actor: Actor) -> Q:
    return Q(created_by=actor.user)


@students.rule(DataScope.DEPARTMENT)
def _department(actor: Actor) -> Q:
    departments = actor.membership.role_assignments.exclude(department="").values("department")
    return Q(section__department__in=departments)
