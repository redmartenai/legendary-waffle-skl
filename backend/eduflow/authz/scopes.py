"""Data scopes: turning "may read students, over my sections" into a queryset (ADR-004).

Each domain module declares a :class:`ScopedResource` for its model and registers one rule per data scope it
supports. A rule returns a ``Q`` that selects the rows that scope covers for an actor::

    students = ScopedResource("student", Student)

    @students.rule(DataScope.SECTION)
    def _taught_sections(actor):
        return Q(section__teacher_assignments__membership=actor.membership)

    @students.rule(DataScope.CHILD)
    def _own_children(actor):
        return Q(guardians__membership=actor.membership)

    qs = students.queryset(actor, "student.read")      # list endpoints
    obj = students.get(actor, "student.read", pk)       # detail endpoints: 404 if out of scope

The resolution is deny-by-default:

* the queryset is always filtered to the actor's school first;
* ``school`` (or ``platform``) scope adds no further filter;
* every other scope adds its rule, and the rules are OR-ed together;
* a scope with no registered rule adds nothing, so an unsupported scope grants no rows;
* no grant at all raises ``PermissionDenied``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from django.db import models
from django.db.models import Q, QuerySet
from rest_framework.exceptions import NotFound, PermissionDenied

from .catalog import DataScope
from .grants import Actor

M = TypeVar("M", bound=models.Model)
ScopeRule = Callable[[Actor], Q]

_UNRESTRICTED = frozenset({DataScope.SCHOOL, DataScope.PLATFORM})


class ScopedResource(Generic[M]):  # noqa: UP046  (django-stubs needs the TypeVar form)
    def __init__(self, name: str, model: type[M], *, school_field: str = "school") -> None:
        self.name = name
        self.model = model
        self.school_field = school_field
        self._rules: dict[DataScope, ScopeRule] = {}

    def rule(self, scope: DataScope) -> Callable[[ScopeRule], ScopeRule]:
        def register(fn: ScopeRule) -> ScopeRule:
            self._rules[scope] = fn
            return fn

        return register

    @property
    def supported_scopes(self) -> frozenset[DataScope]:
        return frozenset(self._rules) | _UNRESTRICTED

    def scope_filter(self, actor: Actor, scopes: frozenset[DataScope]) -> Q | None:
        """``None`` means the whole school; an empty ``Q(pk__in=[])`` means nothing."""
        if scopes & _UNRESTRICTED:
            return None
        combined = Q(pk__in=[])
        for scope in scopes:
            rule = self._rules.get(scope)
            if rule is not None:
                combined |= rule(actor)
        return combined

    def queryset(self, actor: Actor, permission: str, base: QuerySet[M] | None = None) -> QuerySet[M]:
        scopes = actor.scopes(permission)
        if not scopes:
            raise PermissionDenied()
        qs = base if base is not None else self.model._default_manager.all()
        qs = qs.filter(**{f"{self.school_field}_id": actor.school.pk})
        condition = self.scope_filter(actor, scopes)
        if condition is None:
            return qs
        # A rule may follow a to-many relation; filter by primary key so rows are never duplicated.
        return qs.filter(pk__in=qs.filter(condition).values("pk"))

    def get(self, actor: Actor, permission: str, pk: Any, base: QuerySet[M] | None = None) -> M:
        """The object, or 404 if it is in another school, out of scope, or missing (all indistinguishable)."""
        try:
            return self.queryset(actor, permission, base).get(pk=pk)
        except (self.model.DoesNotExist, ValueError, TypeError):  # type: ignore[attr-defined]
            raise NotFound() from None

    def can(self, actor: Actor, permission: str, obj: M) -> bool:
        if not actor.scopes(permission):
            return False
        return self.queryset(actor, permission).filter(pk=obj.pk).exists()
