"""Hostel scopes: hostels and rooms are the office's (school scope); allocations, outpasses and the roll call
follow the student (a parent sees their children's, a student their own)."""

from __future__ import annotations

from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import student_rules

from .models import Allocation, Hostel, Outpass, RollCall, Room

hostels: ScopedResource[Hostel] = ScopedResource("hostel", Hostel)
rooms: ScopedResource[Room] = ScopedResource("hostel_room", Room)
allocations: ScopedResource[Allocation] = ScopedResource("hostel_allocation", Allocation)
outpasses: ScopedResource[Outpass] = ScopedResource("outpass", Outpass)
roll: ScopedResource[RollCall] = ScopedResource("hostel_roll_call", RollCall)

for _resource in (allocations, outpasses, roll):
    student_rules(_resource)
