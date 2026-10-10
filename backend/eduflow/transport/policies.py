"""Transport scopes.

=========  ==========================================================================================
Scope      Covered
=========  ==========================================================================================
assigned   a driver: the routes they drive, and those routes' vehicles, stops, riders and trips
child      a parent: the routes their children ride, and those routes' trips; their children's rides
self       a student: the route they ride and its trips; their own ride
=========  ==========================================================================================
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import children, own_student, student_rules

from .models import Maintenance, Position, Rider, Route, Trip, Vehicle

vehicles: ScopedResource[Vehicle] = ScopedResource("vehicle", Vehicle)
routes: ScopedResource[Route] = ScopedResource("route", Route)
riders: ScopedResource[Rider] = ScopedResource("rider", Rider)
trips: ScopedResource[Trip] = ScopedResource("trip", Trip)
positions: ScopedResource[Position] = ScopedResource("position", Position)
maintenance: ScopedResource[Maintenance] = ScopedResource("maintenance", Maintenance)


def _drives(prefix: str, actor: Actor) -> Q:
    return Q(**{f"{prefix}driver__membership": actor.membership})


def _rides(prefix: str, actor: Actor, family: bool) -> Q:
    who = children if family else own_student
    return who(f"{prefix}riders__student__", actor) & Q(**{f"{prefix}riders__is_active": True})


routes.rule(DataScope.ASSIGNED)(lambda actor: _drives("", actor))
routes.rule(DataScope.CHILD)(lambda actor: _rides("", actor, True))
routes.rule(DataScope.SELF)(lambda actor: _rides("", actor, False))

trips.rule(DataScope.ASSIGNED)(lambda actor: _drives("route__", actor))
trips.rule(DataScope.CHILD)(lambda actor: _rides("route__", actor, True))
trips.rule(DataScope.SELF)(lambda actor: _rides("route__", actor, False))

positions.rule(DataScope.ASSIGNED)(lambda actor: _drives("trip__route__", actor))
positions.rule(DataScope.CHILD)(lambda actor: _rides("trip__route__", actor, True))
positions.rule(DataScope.SELF)(lambda actor: _rides("trip__route__", actor, False))

vehicles.rule(DataScope.ASSIGNED)(lambda actor: _drives("routes__", actor))

student_rules(riders)
riders.rule(DataScope.ASSIGNED)(lambda actor: _drives("route__", actor))
