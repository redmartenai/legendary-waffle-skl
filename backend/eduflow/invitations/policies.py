"""Invitations are school administration records: readable and manageable only school-wide.

No narrower scope has a rule, so a grant with any other scope sees nothing (Phase 2 fail-closed rule).
"""

from __future__ import annotations

from eduflow.authz.scopes import ScopedResource

from .models import Invitation

invitations: ScopedResource[Invitation] = ScopedResource("invitation", Invitation)
