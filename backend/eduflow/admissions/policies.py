"""Admissions are an office workflow: only school-wide grants (school admin, principal) cover them. No
narrower scope has a rule, so a narrower grant sees nothing (fails closed)."""

from __future__ import annotations

from eduflow.authz.scopes import ScopedResource

from .models import Application

applications: ScopedResource[Application] = ScopedResource("application", Application)
