"""Alumni data is the alumni office's: every resource is school scope only."""

from __future__ import annotations

from eduflow.authz.scopes import ScopedResource

from .models import Alumnus, Campaign, Donation, Event

alumni: ScopedResource[Alumnus] = ScopedResource("alumnus", Alumnus)
events: ScopedResource[Event] = ScopedResource("alumni_event", Event)
campaigns: ScopedResource[Campaign] = ScopedResource("campaign", Campaign)
donations: ScopedResource[Donation] = ScopedResource("donation", Donation)
